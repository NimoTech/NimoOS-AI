import asyncio
import pytest
import mcp_client.client as mc


class FakeConn:
    def __init__(self): self.closed = False
    async def aclose(self): self.closed = True


def _reset():
    mc._RUN_CONNS_VAR.set({})
    mc._RUN_CONN_LOCKS_VAR.set({})


@pytest.mark.asyncio
async def test_get_run_conn_connects_once_then_reuses(monkeypatch):
    _reset()
    calls = {"n": 0}
    async def fake_connect(s):
        calls["n"] += 1
        return FakeConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)

    a1 = await mc._get_run_conn({"id": 1, "name": "x"})
    a2 = await mc._get_run_conn({"id": 1, "name": "x"})
    assert a1 is a2 and calls["n"] == 1
    await mc._get_run_conn({"id": 2, "name": "y"})
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_get_run_conn_concurrent_single_flight(monkeypatch):
    _reset()
    calls = {"n": 0}
    async def fake_connect(s):
        calls["n"] += 1
        await asyncio.sleep(0.02)
        return FakeConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    await asyncio.gather(*[mc._get_run_conn({"id": 1, "name": "x"}) for _ in range(5)])
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_close_run_conns_closes_and_clears(monkeypatch):
    _reset()
    async def fake_connect(s): return FakeConn()
    monkeypatch.setattr(mc, "_connect", fake_connect)
    c = await mc._get_run_conn({"id": 1, "name": "x"})
    await mc.close_run_conns()
    assert c.closed is True
    assert mc._RUN_CONNS_VAR.get() == {}
