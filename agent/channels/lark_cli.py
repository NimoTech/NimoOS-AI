"""M4:lark-cli 进程层 —— 只管进程,不管 channel 语义。

两种形态,来自 spec §1 的 spike 实测:

* **一次性**(出站):每发一条消息起一个短命子进程,带墙钟超时。
* **常驻**(入站):`event consume <key>` 逐行吐 NDJSON。这里有三条不直观的规矩:

  1. **ready 看 stderr 标记,不看 spawn 成功。** lark-cli 在连上飞书长连接后才
     打印 `[event] ready event_key=...`;在此之前进程活着但没人在听,把它当就绪
     会让确认卡发出去却收不到点击。
  2. **停止只发 SIGTERM。** CLI 自己警告 `kill -9` 会跳过清理并泄漏服务端订阅
     (要人去开发者后台清)。所以赖着不走的子进程只能记 error 放弃 —— 留一个
     进程等容器重启回收,比留一个泄漏的订阅便宜。
  3. **bus daemon 不用管。** 它由 lark-cli 自己拉起,并在最后一个消费者退出 30
     秒后自行回收。
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from lark import binding as lark_binding

_LOG = logging.getLogger("nimoos-agent.channels.lark_cli")

# Sentinel return codes, distinct from any exit status a CLI would produce.
RC_NO_CLI = -101
RC_TIMEOUT = -102

# The stderr line that means the long connection is up (spike-verified).
READY_PREFIX = "[event] ready"

# How long stop() waits after SIGTERM before giving up. Never escalates.
STOP_GRACE_SECONDS = 10.0

# A single NDJSON line longer than this is dropped rather than buffered — a
# runaway line must not become unbounded memory in a long-lived process.
MAX_LINE_BYTES = 1_000_000


async def _spawn(uid: str, args: list[str]):
    """create_subprocess_exec with the minimal per-user env, or None if absent."""
    try:
        return await asyncio.create_subprocess_exec(
            lark_binding.lark_bin(), *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=lark_binding.cli_env(uid),
            cwd=str(lark_binding.user_home(uid)),
            limit=MAX_LINE_BYTES,
        )
    except OSError:
        return None


async def run_once(uid: str, args: list[str], *,
                   timeout: float = 20.0) -> tuple[int, str, str]:
    """Run the CLI to completion. Returns (rc, stdout, stderr); never raises."""
    proc = await _spawn(uid, args)
    if proc is None:
        return RC_NO_CLI, "", f"lark-cli not found at {lark_binding.lark_bin()}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # A one-shot send is not an event subscription, so terminating it
        # leaks nothing; still prefer TERM over KILL for consistency.
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            _LOG.error("lark-cli one-shot ignored SIGTERM; abandoning pid %s",
                       proc.pid)
        return RC_TIMEOUT, "", f"timed out after {timeout}s"
    return (proc.returncode or 0,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace"))


class Consumer:
    """A supervised, long-lived `lark-cli event consume` process.

    `on_event(dict)` is called for each NDJSON line. It must not raise and must
    not block; an exception is logged and swallowed so one bad event cannot end
    the subscription.
    """

    def __init__(self, uid: str, event_key: str, on_event,
                 *, on_state=None, backoff_cap: float = 60.0):
        self._uid = uid
        self._key = event_key
        self._on_event = on_event
        self._on_state = on_state
        self._backoff_cap = backoff_cap
        self._task: asyncio.Task | None = None
        self._proc = None
        self._ready = False
        self._stopping = False

    @property
    def ready(self) -> bool:
        return self._ready

    def _set_ready(self, value: bool) -> None:
        if self._ready == value:
            return
        self._ready = value
        if self._on_state is not None:
            try:
                self._on_state(value)
            except Exception:            # noqa: BLE001
                _LOG.exception("lark consumer on_state raised")

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
        await self._terminate_child()
        self._set_ready(False)

    async def _terminate_child(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=STOP_GRACE_SECONDS)
        except asyncio.TimeoutError:
            # Deliberately NOT proc.kill(): SIGKILL skips lark-cli's cleanup
            # and leaks the server-side subscription, which then needs manual
            # removal in the Feishu console. An abandoned child dies with the
            # container; a leaked subscription does not.
            _LOG.error("lark consumer ignored SIGTERM after %.0fs; abandoning "
                       "pid %s WITHOUT SIGKILL (would leak the subscription)",
                       STOP_GRACE_SECONDS, proc.pid)

    async def _supervise(self) -> None:
        # Clamped by backoff_cap: with the default 60s cap this is a no-op in
        # production, but a test that passes a small backoff_cap (e.g. to keep
        # a restart test fast) must not still eat a full unclamped 1s first
        # sleep — the cap is meant to bound every delay, including the first.
        delay = min(1.0, self._backoff_cap)
        while not self._stopping:
            started = await self._run_once_streaming()
            if self._stopping:
                return
            # Reset the backoff only if the last attempt actually reached ready;
            # a process that dies before ready is a real failure (bad config,
            # missing console subscription) and must back off.
            delay = min(1.0, self._backoff_cap) if started else min(delay * 2, self._backoff_cap)
            await asyncio.sleep(delay)

    async def _run_once_streaming(self) -> bool:
        args = ["event", "consume", self._key, "--as", "bot"]
        proc = await _spawn(self._uid, args)
        if proc is None:
            _LOG.warning("lark consumer: lark-cli not found at %s", lark_binding.lark_bin())
            return False
        self._proc = proc
        reached_ready = False
        err_task = asyncio.create_task(self._watch_stderr(proc))
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                reached_ready = reached_ready or self._ready
                self._emit(line)
        except asyncio.LimitOverrunError:
            _LOG.warning("lark consumer: oversized line dropped")
        except asyncio.CancelledError:
            raise
        except Exception:                # noqa: BLE001
            _LOG.exception("lark consumer: stdout loop failed")
        finally:
            err_task.cancel()
            self._set_ready(False)
            with_rc = proc.returncode
            if with_rc is None:
                await self._terminate_child()
        return reached_ready or self._ready

    async def _watch_stderr(self, proc) -> None:
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text.startswith(READY_PREFIX):
                    self._set_ready(True)
                # lark-cli's stderr is its own diagnostic channel; keep it at
                # debug so a healthy subscription does not spam the log.
                _LOG.debug("lark consumer stderr: %s", text)
        except asyncio.CancelledError:
            return
        except Exception:                # noqa: BLE001
            _LOG.exception("lark consumer: stderr loop failed")

    def _emit(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            ev = json.loads(text)
        except (ValueError, TypeError):
            _LOG.warning("lark consumer: dropping non-JSON line (%d bytes)",
                         len(raw))
            return
        if not isinstance(ev, dict):
            _LOG.warning("lark consumer: dropping non-object line")
            return
        try:
            self._on_event(ev)
        except Exception:                # noqa: BLE001
            _LOG.exception("lark consumer: on_event raised; event dropped")
