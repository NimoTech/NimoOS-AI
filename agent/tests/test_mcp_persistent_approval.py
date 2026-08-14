import asyncio
import mcp_client.client as mc


class _Mgr:
    def __init__(self, remember): self._r = remember
    def register(self, *a, **kw): return "cid"
    async def wait(self, cid): return True
    def consume_remember(self, cid): return self._r


class _Q:
    def __init__(self): self.items = []
    async def put(self, e): self.items.append(e)


def _setup(monkeypatch, remember, approvals=frozenset()):
    mc.CONFIRM_MGR_VAR.set(_Mgr(remember))
    q = _Q(); mc.EVENT_QUEUE_VAR.set(q)
    mc.SESSION_ID_VAR.set("s1")
    mc._CONFIRMED_TOOLS_VAR.set(set(approvals))
    mc.WRITE_TOKEN_VAR.set("tok")
    written = []
    async def fake_put(token, sid, tool):
        written.append((token, sid, tool)); return True
    monkeypatch.setattr("mcp_client.runtime.put_approval", fake_put)
    return q, written


def test_prefetched_approval_skips_the_card(monkeypatch):
    q, _ = _setup(monkeypatch, remember=False, approvals={"1::search"})
    ok = asyncio.run(mc._ensure_confirmed({"id": 1, "name": "s"}, "search", {}))
    assert ok is True
    assert q.items == [], "a persisted approval must not raise a confirm card at all"


def test_wildcard_approval_covers_any_tool(monkeypatch):
    q, _ = _setup(monkeypatch, remember=False, approvals={"1::*"})
    assert asyncio.run(mc._ensure_confirmed({"id": 1, "name": "s"}, "anything", {})) is True
    assert q.items == []


def test_remember_persists_via_write_token(monkeypatch):
    _, written = _setup(monkeypatch, remember=True)
    asyncio.run(mc._ensure_confirmed({"id": 1, "name": "s"}, "search", {}))
    assert written == [("tok", 1, "search")]


def test_persist_failure_does_not_block_the_call(monkeypatch):
    """Degradation rule (non-negotiable): a persist failure must never block this call."""
    _setup(monkeypatch, remember=True)
    async def boom(*a, **kw): return False
    monkeypatch.setattr("mcp_client.runtime.put_approval", boom)
    assert asyncio.run(mc._ensure_confirmed({"id": 1, "name": "s"}, "search", {})) is True


def test_no_background_refresh_symbols_remain():
    """Go is the sole writer; Python must not have any background refresh task."""
    for gone in ("_schedule_revalidate", "_revalidate", "_REVALIDATING",
                 "_BACKGROUND_TASKS", "_cold_fetch"):
        assert not hasattr(mc, gone), f"{gone} must be removed — Go owns refreshing now"
