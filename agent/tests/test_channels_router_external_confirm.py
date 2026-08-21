"""surface_external_confirm — the three-button card path a scheduled task's
escalation renders through the router (Allow once / Deny / Allow & persist)."""
import asyncio

import pytest

from channels.router import ChannelRouter


class FakeAdapter:
    instance_id = "inst1"

    class capabilities:
        supports_buttons = True

    def __init__(self, message_id="m1", fail=False):
        self._message_id = message_id
        self._fail = fail
        self.sent = []
        self.resolved = []

    async def send_buttons(self, chat_id, text, buttons):
        if self._fail:
            raise RuntimeError("boom")
        self.sent.append((chat_id, text, buttons))
        return self._message_id

    async def edit_to_resolved(self, chat_id, message_id, text):
        self.resolved.append((chat_id, message_id, text))


def _router(resolutions):
    def resolve(cid, confirmed, expected_session_id=None, **kw):
        resolutions.append((cid, confirmed, expected_session_id))
    return ChannelRouter(None, start_run=None, cancel_run=None,
                         resolve_credentials=None, resolve_confirm=resolve,
                         confirm_timeout=60.0)


@pytest.mark.asyncio
async def test_three_buttons_sent_with_persist_label():
    router = _router([])
    adapter = FakeAdapter()
    ok = await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "Task X wants Y",
        persist_label="Allow & add")
    assert ok is True
    (_, _, buttons), = adapter.sent
    assert [b[1] for b in buttons] == ["cf:cid1:a", "cf:cid1:d", "cf:cid1:p"]


@pytest.mark.asyncio
async def test_two_buttons_without_persist_label():
    router = _router([])
    adapter = FakeAdapter()
    await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t")
    (_, _, buttons), = adapter.sent
    assert [b[1] for b in buttons] == ["cf:cid1:a", "cf:cid1:d"]


@pytest.mark.asyncio
async def test_persist_click_resolves_true_and_reports_persist():
    resolutions, outcomes = [], []
    router = _router(resolutions)
    adapter = FakeAdapter()
    await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t",
        persist_label="Allow & add",
        on_resolved=lambda allow, persist: outcomes.append((allow, persist)))
    await router.handle_confirm(adapter, "chat1", "cf:cid1:p")
    await asyncio.sleep(0)
    assert resolutions == [("cid1", True, "sess1")]
    assert outcomes == [(True, True)]
    assert "pre-authorization" in adapter.resolved[0][2]


@pytest.mark.asyncio
async def test_allow_click_reports_no_persist():
    resolutions, outcomes = [], []
    router = _router(resolutions)
    adapter = FakeAdapter()
    await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t",
        on_resolved=lambda allow, persist: outcomes.append((allow, persist)))
    await router.handle_confirm(adapter, "chat1", "cf:cid1:a")
    await asyncio.sleep(0)
    assert resolutions == [("cid1", True, "sess1")]
    assert outcomes == [(True, False)]


@pytest.mark.asyncio
async def test_deny_click_reports_no_persist():
    resolutions, outcomes = [], []
    router = _router(resolutions)
    adapter = FakeAdapter()
    await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t",
        on_resolved=lambda allow, persist: outcomes.append((allow, persist)))
    await router.handle_confirm(adapter, "chat1", "cf:cid1:d")
    await asyncio.sleep(0)
    assert resolutions == [("cid1", False, "sess1")]
    assert outcomes == [(False, False)]


@pytest.mark.asyncio
async def test_send_failure_returns_false_and_registers_nothing():
    router = _router([])
    adapter = FakeAdapter(fail=True)
    ok = await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t")
    assert ok is False
    assert router._confirms == {}


@pytest.mark.asyncio
async def test_none_message_id_returns_false():
    router = _router([])
    adapter = FakeAdapter(message_id=None)
    ok = await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t")
    assert ok is False
    assert router._confirms == {}


@pytest.mark.asyncio
async def test_timeout_denies_and_reports():
    resolutions, outcomes = [], []
    router = _router(resolutions)
    adapter = FakeAdapter()
    await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t", timeout=0.01,
        on_resolved=lambda allow, persist: outcomes.append((allow, persist)))
    await asyncio.sleep(0.05)
    assert resolutions == [("cid1", False, "sess1")]
    assert outcomes == [(False, False)]
    assert adapter.resolved          # card rewritten with the timed-out text


@pytest.mark.asyncio
async def test_unknown_suffix_ignored():
    router = _router([])
    adapter = FakeAdapter()
    await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t")
    await router.handle_confirm(adapter, "chat1", "cf:cid1:x")
    assert "cid1" in router._confirms      # entry untouched
    router._confirms["cid1"]["timer"].cancel()


@pytest.mark.asyncio
async def test_stale_persist_click_is_ignored():
    router = _router([])
    adapter = FakeAdapter()
    await router.handle_confirm(adapter, "chat1", "cf:ghost:p")  # no crash


@pytest.mark.asyncio
async def test_clear_confirms_for_session_fires_outcome():
    resolutions, outcomes = [], []
    router = _router(resolutions)
    adapter = FakeAdapter()
    await router.surface_external_confirm(
        adapter, "chat1", "sess1", "cid1", "t",
        on_resolved=lambda allow, persist: outcomes.append((allow, persist)))
    router._clear_confirms_for_session("sess1")
    assert outcomes == [(False, False)]
