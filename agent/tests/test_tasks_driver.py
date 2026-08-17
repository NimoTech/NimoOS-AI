"""TaskRunDriver — headless consumption of a RunSink for scheduled tasks.

The harness is deliberately the same 15-line FakeSink used by
tests/test_channels_driver.py: subscribe() hands back (past, queue) and
unsubscribe() just records that it happened.
"""
import asyncio
import os

import pytest

from tasks.driver import TaskRunDriver

SID = "sess-1"


class FakeSink:
    def __init__(self, past=(), live=()):
        self._past = list(past)
        self._live = list(live)
        self.unsubscribed = False

    def subscribe(self):
        q = asyncio.Queue()
        for ev in self._live:
            q.put_nowait(ev)
        return list(self._past), q

    def unsubscribe(self, q):
        self.unsubscribed = True


class FakeConfirmManager:
    """Records resolve() calls. `raise_on` makes a given confirm_id raise
    KeyError, the shape ConfirmManager.resolve uses for expired / mis-routed
    confirmations."""

    def __init__(self, raise_on=()):
        self.calls = []          # [(confirm_id, confirmed, expected_session_id)]
        self.cancelled = []      # [session_id]
        self._raise_on = set(raise_on)

    def resolve(self, confirm_id, confirmed, remember=False,
                expected_session_id=None, *, action=None, content=None):
        self.calls.append((confirm_id, confirmed, expected_session_id))
        if confirm_id in self._raise_on:
            raise KeyError("confirm_expired")

    def cancel_session(self, session_id):
        self.cancelled.append(session_id)
        return 0


def _driver(mgr, preauth=None, run_timeout=600.0, now=None):
    async def no_sleep(_):
        return None

    return TaskRunDriver(confirm_mgr=mgr, session_id=SID,
                         preauth=preauth or {}, run_timeout=run_timeout,
                         sleep=no_sleep, now=now)


# --------------------------------------------------------------------------
# Contract 1: text accumulation
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accumulates_message_deltas_until_done():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[
        {"type": "message_delta", "content": "Report: "},
        {"type": "tool_call"},
        {"type": "message_delta", "content": "all green."},
        {"type": "done"}])
    out = await _driver(mgr).drive(sink)
    assert out["status"] == "succeeded"
    assert out["summary"] == "Report: all green."
    assert out["error"] == ""
    assert out["denied"] == [] and out["auto_approved"] == []
    assert sink.unsubscribed is True


@pytest.mark.asyncio
async def test_terminal_message_event_overrides_delta_buffer():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[
        {"type": "message_delta", "content": "partial junk"},
        {"type": "message", "content": "final answer"},
        {"type": "done"}])
    out = await _driver(mgr).drive(sink)
    assert out["summary"] == "final answer"


@pytest.mark.asyncio
async def test_live_queue_events_are_consumed_after_past_replay():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[{"type": "message_delta", "content": "a"}],
                    live=[{"type": "message_delta", "content": "b"},
                          {"type": "done"}])
    out = await _driver(mgr).drive(sink)
    assert out["summary"] == "ab"
    assert out["status"] == "succeeded"


# --------------------------------------------------------------------------
# Contract 2a: egress confirmations
# --------------------------------------------------------------------------

def _egress(host, cid="c1"):
    return {"type": "confirmation_required", "confirm_id": cid,
            "action": "egress_confirm", "host": host, "bytes": 1234,
            "reason": "policy", "description": f"Outbound to {host!r}"}


@pytest.mark.asyncio
async def test_egress_exact_domain_is_auto_approved_port_and_case_ignored():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[_egress("Open.Feishu.CN:443"), {"type": "done"}])
    out = await _driver(mgr, {"egress_domains": ["open.feishu.cn"]}).drive(sink)
    assert mgr.calls == [("c1", True, SID)]
    assert out["denied"] == []
    assert out["auto_approved"] == [{"kind": "egress", "detail": "Open.Feishu.CN:443"}]
    assert out["status"] == "succeeded"


@pytest.mark.asyncio
async def test_egress_wildcard_matches_subdomain_only():
    preauth = {"egress_domains": ["*.example.com"]}

    mgr = FakeConfirmManager()
    await _driver(mgr, preauth).drive(
        FakeSink(past=[_egress("api.example.com"), {"type": "done"}]))
    assert mgr.calls == [("c1", True, SID)]

    # bare apex is NOT covered by *.example.com
    mgr2 = FakeConfirmManager()
    await _driver(mgr2, preauth).drive(
        FakeSink(past=[_egress("example.com"), {"type": "done"}]))
    assert mgr2.calls == [("c1", False, SID)]

    # suffix-confusion: evil-example.com must not match
    mgr3 = FakeConfirmManager()
    await _driver(mgr3, preauth).drive(
        FakeSink(past=[_egress("evil-example.com"), {"type": "done"}]))
    assert mgr3.calls == [("c1", False, SID)]


@pytest.mark.asyncio
async def test_egress_unlisted_host_is_denied_and_recorded():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[_egress("evil.example.org"), {"type": "done"}])
    out = await _driver(mgr, {"egress_domains": ["open.feishu.cn"]}).drive(sink)
    assert mgr.calls == [("c1", False, SID)]
    assert out["denied"] == [{"kind": "egress", "detail": "evil.example.org"}]
    assert out["auto_approved"] == []
    assert out["status"] == "succeeded"          # a denial is not a run failure


@pytest.mark.asyncio
async def test_egress_with_empty_preauth_is_denied():
    mgr = FakeConfirmManager()
    out = await _driver(mgr).drive(
        FakeSink(past=[_egress("open.feishu.cn"), {"type": "done"}]))
    assert mgr.calls == [("c1", False, SID)]
    assert out["denied"] == [{"kind": "egress", "detail": "open.feishu.cn"}]


# --------------------------------------------------------------------------
# Contract 2b: fs access_request
# --------------------------------------------------------------------------

def _access(path, cid="c1", **extra):
    ev = {"type": "access_request", "confirm_id": cid, "path": path,
          "kind": "folder", "reason": "Needs to create or modify files inside",
          "reason_key": "write"}
    ev.update(extra)
    return ev


@pytest.mark.asyncio
async def test_fs_path_inside_preauth_dir_is_approved(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[_access(str(root / "2026-08" / "a.md")), {"type": "done"}])
    out = await _driver(mgr, {"fs_write": [str(root)]}).drive(sink)
    assert mgr.calls == [("c1", True, SID)]
    assert out["auto_approved"] == [
        {"kind": "fs", "detail": str(root / "2026-08" / "a.md")}]


@pytest.mark.asyncio
async def test_fs_sibling_prefix_directory_is_denied(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    evil = tmp_path / "reports-evil"
    evil.mkdir()
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[_access(str(evil / "x.md")), {"type": "done"}])
    out = await _driver(mgr, {"fs_write": [str(root)]}).drive(sink)
    assert mgr.calls == [("c1", False, SID)]
    assert out["denied"] == [{"kind": "fs", "detail": str(evil / "x.md")}]


@pytest.mark.asyncio
async def test_fs_symlink_into_the_root_is_approved(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    link = tmp_path / "link"
    os.symlink(str(root), str(link))
    mgr = FakeConfirmManager()
    await _driver(mgr, {"fs_write": [str(root)]}).drive(
        FakeSink(past=[_access(str(link / "a.md")), {"type": "done"}]))
    assert mgr.calls == [("c1", True, SID)]


@pytest.mark.asyncio
async def test_fs_symlink_escaping_the_root_is_denied(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    os.symlink(str(secret), str(root / "out"))
    mgr = FakeConfirmManager()
    await _driver(mgr, {"fs_write": [str(root)]}).drive(
        FakeSink(past=[_access(str(root / "out" / "creds")), {"type": "done"}]))
    assert mgr.calls == [("c1", False, SID)]


@pytest.mark.asyncio
async def test_fs_batch_card_requires_every_path_inside(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    mgr = FakeConfirmManager()
    await _driver(mgr, {"fs_write": [str(root)]}).drive(FakeSink(past=[
        _access(str(root / "a"), paths=[str(root / "a"), str(root / "b")]),
        {"type": "done"}]))
    assert mgr.calls == [("c1", True, SID)]

    mgr2 = FakeConfirmManager()
    await _driver(mgr2, {"fs_write": [str(root)]}).drive(FakeSink(past=[
        _access(str(root / "a"), paths=[str(root / "a"), str(outside / "b")]),
        {"type": "done"}]))
    assert mgr2.calls == [("c1", False, SID)]


@pytest.mark.asyncio
async def test_fs_empty_path_is_denied():
    mgr = FakeConfirmManager()
    await _driver(mgr, {"fs_write": ["/DATA"]}).drive(
        FakeSink(past=[_access(""), {"type": "done"}]))
    assert mgr.calls == [("c1", False, SID)]


# --------------------------------------------------------------------------
# Contract 2c: everything else is denied
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shell_and_mcp_cards_are_denied_with_detail():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[
        {"type": "confirmation_required", "confirm_id": "c1",
         "action": "shell_command", "command": "rm -rf /DATA",
         "description": "Agent requests to run a command"},
        {"type": "confirmation_required", "confirm_id": "c2",
         "kind": "mcp_tool", "server": "lark", "tool": "send_message"},
        {"type": "confirmation_required", "confirm_id": "c3",
         "kind": "toolbox_install", "title": "Install ripgrep v14",
         "detail": "fast search"},
        {"type": "done"}])
    out = await _driver(mgr, {"egress_domains": ["*"], "fs_write": ["/"]}).drive(sink)
    assert mgr.calls == [("c1", False, SID), ("c2", False, SID), ("c3", False, SID)]
    assert out["denied"] == [
        {"kind": "shell", "detail": "rm -rf /DATA"},
        {"kind": "mcp_tool", "detail": "lark::send_message"},
        {"kind": "toolbox_install", "detail": "Install ripgrep v14"},
    ]


@pytest.mark.asyncio
async def test_mcp_elicit_url_card_is_not_treated_as_egress():
    # An elicitation card carries a `host` field but is NOT an egress confirm;
    # keying on `action == "egress_confirm"` is what keeps them apart.
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[
        {"type": "confirmation_required", "confirm_id": "c1",
         "kind": "mcp_elicit_url", "server": "lark",
         "host": "open.feishu.cn", "url": "https://open.feishu.cn/auth",
         "message": "authorize"},
        {"type": "done"}])
    out = await _driver(mgr, {"egress_domains": ["open.feishu.cn"]}).drive(sink)
    assert mgr.calls == [("c1", False, SID)]
    assert out["denied"] == [{"kind": "mcp_elicit_url", "detail": "open.feishu.cn"}]


# --------------------------------------------------------------------------
# Contract 2d: resolve() failures never kill the driver
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_keyerror_is_swallowed_and_recorded_as_denied():
    mgr = FakeConfirmManager(raise_on=["c1"])
    sink = FakeSink(past=[_egress("open.feishu.cn"), {"type": "message",
                                                     "content": "done anyway"},
                          {"type": "done"}])
    out = await _driver(mgr, {"egress_domains": ["open.feishu.cn"]}).drive(sink)
    assert mgr.calls == [("c1", True, SID)]          # we did try to approve
    assert out["denied"] == [{"kind": "egress", "detail": "open.feishu.cn"}]
    assert out["auto_approved"] == []                # the approval never landed
    assert out["status"] == "succeeded"
    assert out["summary"] == "done anyway"


@pytest.mark.asyncio
async def test_confirm_without_confirm_id_is_recorded_but_not_resolved():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[
        {"type": "confirmation_required", "action": "egress_confirm",
         "host": "evil.example.org"},
        {"type": "done"}])
    out = await _driver(mgr).drive(sink)
    assert mgr.calls == []
    assert out["denied"] == [{"kind": "egress", "detail": "evil.example.org"}]


@pytest.mark.asyncio
async def test_driver_never_blocks_on_a_confirmation():
    # The whole point: a confirm must be answered inline. If the driver ever
    # awaited a human, this run (whose `done` follows immediately) would hang.
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[_egress("open.feishu.cn")],
                    live=[{"type": "message", "content": "ok"}, {"type": "done"}])
    out = await asyncio.wait_for(
        _driver(mgr, {"egress_domains": ["open.feishu.cn"]}).drive(sink),
        timeout=2.0)
    assert out["summary"] == "ok"


# --------------------------------------------------------------------------
# Contracts 3, 4, 6: error / max_turns / empty
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_error_event_fails_the_run():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[{"type": "message_delta", "content": "half"},
                          {"type": "error", "content": "boom"},
                          {"type": "done"}])
    out = await _driver(mgr).drive(sink)
    assert out["status"] == "failed"
    assert out["error"] == "boom"
    assert out["summary"] == "half"


@pytest.mark.asyncio
async def test_max_turns_exceeded_with_body_still_succeeds_but_keeps_the_note():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[{"type": "message_delta", "content": "partial report"},
                          {"type": "max_turns_exceeded", "max_turns": 30},
                          {"type": "done"}])
    out = await _driver(mgr).drive(sink)
    assert out["status"] == "succeeded"
    assert out["summary"] == "partial report"
    assert "max_turns_exceeded" in out["error"] and "30" in out["error"]


@pytest.mark.asyncio
async def test_max_turns_exceeded_without_body_fails():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[{"type": "max_turns_exceeded", "max_turns": 30},
                          {"type": "done"}])
    out = await _driver(mgr).drive(sink)
    assert out["status"] == "failed"
    assert "max_turns_exceeded" in out["error"]


@pytest.mark.asyncio
async def test_empty_run_is_succeeded_not_failed():
    mgr = FakeConfirmManager()
    out = await _driver(mgr).drive(FakeSink(past=[{"type": "done"}]))
    assert out == {"status": "succeeded", "summary": "", "error": "",
                   "denied": [], "auto_approved": []}


# --------------------------------------------------------------------------
# Contract 5: timeout + cleanup
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_uses_absolute_deadline_and_cleans_up():
    mgr = FakeConfirmManager()
    ticks = iter([0.0, 100.0, 100.0, 100.0])       # first call sets deadline
    sink = FakeSink(past=[{"type": "message_delta", "content": "started"}])
    out = await _driver(mgr, run_timeout=10.0,
                        now=lambda: next(ticks)).drive(sink)
    assert out["status"] == "timeout"
    assert out["summary"] == "started"
    assert out["error"] == "timeout"
    assert sink.unsubscribed is True
    assert mgr.cancelled == [SID]                  # pending confirms released


@pytest.mark.asyncio
async def test_cancel_session_and_unsubscribe_also_run_on_the_happy_path():
    mgr = FakeConfirmManager()
    sink = FakeSink(past=[{"type": "done"}])
    await _driver(mgr).drive(sink)
    assert sink.unsubscribed is True
    assert mgr.cancelled == [SID]


@pytest.mark.asyncio
async def test_cleanup_runs_even_if_the_sink_raises():
    class ExplodingSink(FakeSink):
        def subscribe(self):
            past, q = super().subscribe()
            raise RuntimeError("sink gone")

    mgr = FakeConfirmManager()
    with pytest.raises(RuntimeError):
        await _driver(mgr).drive(ExplodingSink(past=[{"type": "done"}]))
    assert mgr.cancelled == [SID]
