# NimoOS-AI/agent/tests/test_channels_lark_cli.py
"""M4 第一段:lark-cli 进程层。

**不调真 lark-cli、不打真网络。** 每个测试自己写一个假 CLI 到 tmp_path 并让
`lark_bin()` 指向它,所以这里断言的是我们对进程的处理,而不是飞书的行为。

两条来自 spike 的硬约束在这里被钉住:
* 常驻消费者只有在 stderr 上出现 `[event] ready` 之后才算 ready —— spawn 成功
  不算,否则卡片发出去了却没人在听点击;
* 停止只发 SIGTERM。lark-cli 自己警告 kill -9 会泄漏服务端订阅,所以即使子进程
  赖着不走,也只能记日志放弃,不能升级信号。
"""
import asyncio
import os
import sys
import pathlib
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from channels import lark_cli
from lark import binding as lark_binding


def _write_stub(tmp_path, body: str) -> pathlib.Path:
    """Write an executable fake lark-cli and point lark_bin() at it."""
    p = tmp_path / "lark-cli"
    p.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    p.chmod(0o755)
    return p


@pytest.fixture
def stub(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(lark_binding, "user_home", lambda uid: home)

    def _install(body: str):
        path = _write_stub(tmp_path, body)
        monkeypatch.setattr(lark_binding, "lark_bin", lambda: str(path))
        return path

    return _install


# ---- run_once ------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_once_returns_rc_stdout_stderr(stub):
    stub("""
        import sys
        print("out-line")
        print("err-line", file=sys.stderr)
        sys.exit(3)
        """)
    rc, out, err = await lark_cli.run_once("1", ["im", "+messages-send"])
    assert rc == 3
    assert "out-line" in out
    assert "err-line" in err


@pytest.mark.asyncio
async def test_run_once_passes_the_per_user_home_and_never_the_agent_env(stub, monkeypatch):
    """The agent process carries provider API keys and DB paths; none of it may
    reach a third-party CLI. And HOME decides WHICH Feishu app is used."""
    monkeypatch.setenv("NIMOOS_SECRET_PROBE", "must-not-leak")
    stub("""
        import json, os, sys
        json.dump({"HOME": os.environ.get("HOME"),
                   "keys": sorted(os.environ)}, sys.stdout)
        """)
    rc, out, _ = await lark_cli.run_once("1", ["config", "show"])
    assert rc == 0
    seen = __import__("json").loads(out)
    assert seen["HOME"] == str(lark_binding.user_home("1"))
    assert "NIMOOS_SECRET_PROBE" not in seen["keys"]


@pytest.mark.asyncio
async def test_run_once_missing_cli_is_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(lark_binding, "lark_bin", lambda: str(tmp_path / "nope"))
    monkeypatch.setattr(lark_binding, "user_home", lambda uid: tmp_path)
    rc, out, err = await lark_cli.run_once("1", ["config", "show"])
    assert rc == lark_cli.RC_NO_CLI
    assert out == ""
    assert "not found" in err.lower()


@pytest.mark.asyncio
async def test_run_once_times_out_without_hanging(stub):
    stub("""
        import time
        time.sleep(30)
        """)
    rc, _out, err = await lark_cli.run_once("1", ["x"], timeout=0.3)
    assert rc == lark_cli.RC_TIMEOUT
    assert "timed out" in err.lower()


# ---- Consumer ------------------------------------------------------------

@pytest.mark.asyncio
async def test_consumer_is_not_ready_until_the_marker_appears(stub):
    stub("""
        import sys, time
        time.sleep(0.25)
        print("[event] ready event_key=card.action.trigger", file=sys.stderr, flush=True)
        time.sleep(5)
        """)
    got = []
    c = lark_cli.Consumer("1", "card.action.trigger", lambda ev: got.append(ev))
    await c.start()
    try:
        assert c.ready is False          # spawned, but nothing is listening yet
        # The child sleeps 0.25s before it ever prints the marker, but the
        # subprocess itself is up in ~10ms (measured). Checking mid-way
        # through that gap pins "ready must come from the marker" against a
        # mutant that flips ready as soon as the child process exists: such a
        # mutant would already read True here, well before the marker can
        # possibly have been read.
        await asyncio.sleep(0.1)
        assert c.ready is False          # still not ready: marker not printed yet
        await asyncio.sleep(0.6)
        assert c.ready is True
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_consumer_delivers_each_ndjson_line(stub):
    stub("""
        import sys, time
        print("[event] ready event_key=k", file=sys.stderr, flush=True)
        print('{"n": 1}', flush=True)
        print('{"n": 2}', flush=True)
        time.sleep(5)
        """)
    got = []
    c = lark_cli.Consumer("1", "k", lambda ev: got.append(ev))
    await c.start()
    try:
        await asyncio.sleep(0.6)
        assert got == [{"n": 1}, {"n": 2}]
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_one_bad_line_does_not_stop_the_stream(stub):
    stub("""
        import sys, time
        print("[event] ready event_key=k", file=sys.stderr, flush=True)
        print('not json', flush=True)
        print('{"n": 2}', flush=True)
        time.sleep(5)
        """)
    got = []
    c = lark_cli.Consumer("1", "k", lambda ev: got.append(ev))
    await c.start()
    try:
        await asyncio.sleep(0.6)
        assert got == [{"n": 2}]
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_consumer_restarts_after_the_process_exits(stub):
    """A crashed consumer must come back, and must report not-ready in between."""
    marker = None
    stub("""
        import os, sys, time
        n = os.environ["HOME"] + "/runs"
        c = 0
        try:
            c = int(open(n).read())
        except Exception:
            pass
        open(n, "w").write(str(c + 1))
        print("[event] ready event_key=k", file=sys.stderr, flush=True)
        if c == 0:
            sys.exit(1)          # first run dies
        time.sleep(5)
        """)
    c = lark_cli.Consumer("1", "k", lambda ev: None, backoff_cap=0.2)
    await c.start()
    try:
        await asyncio.sleep(1.2)
        runs = int((lark_binding.user_home("1") / "runs").read_text())
        assert runs >= 2, "consumer did not restart"
        assert c.ready is True
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_stop_uses_sigterm_and_never_escalates(stub, monkeypatch):
    """kill -9 leaks server-side subscriptions, so a stubborn child is logged
    and abandoned — never killed."""
    stub("""
        import signal, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("[event] ready event_key=k", file=sys.stderr, flush=True)
        time.sleep(30)
        """)
    monkeypatch.setattr(lark_cli, "STOP_GRACE_SECONDS", 0.3)
    killed = []
    c = lark_cli.Consumer("1", "k", lambda ev: None)
    await c.start()
    await asyncio.sleep(0.4)
    proc = c._proc                                   # noqa: SLF001 — asserting no kill()
    monkeypatch.setattr(proc, "kill", lambda: killed.append(True))

    await c.stop()

    assert killed == [], "stop() must not escalate to SIGKILL"
    assert c.ready is False


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_survives_a_missing_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(lark_binding, "lark_bin", lambda: str(tmp_path / "nope"))
    monkeypatch.setattr(lark_binding, "user_home", lambda uid: tmp_path)
    c = lark_cli.Consumer("1", "k", lambda ev: None)
    await c.start()          # CLI absent: must not raise
    assert c.ready is False
    await c.stop()
    await c.stop()           # second stop must be a no-op
