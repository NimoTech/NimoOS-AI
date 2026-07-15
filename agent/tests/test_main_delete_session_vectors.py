import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch


@pytest_asyncio.fixture
async def client_with_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    import importlib
    import sys
    # Clean up any cached modules so main.py re-reads AGENT_DB_PATH.
    for mod in ["main", "agent", "db"]:
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        r = await c.post("/agent/sessions", headers={"X-User-Id": "u1"})
        session_id = r.json()["session_id"]
        yield c, session_id


@pytest.mark.asyncio
async def test_delete_session_purges_vectors(client_with_session):
    client, session_id = client_with_session
    mock_pc = AsyncMock()
    with patch("recall_index._get_parser_client", return_value=mock_pc):
        r = await client.delete(f"/agent/sessions/{session_id}",
                                headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    mock_pc.agent_memory_delete.assert_awaited_once_with("u1", session_id)


@pytest.mark.asyncio
async def test_delete_session_survives_parser_down(client_with_session):
    client, session_id = client_with_session
    mock_pc = AsyncMock()
    mock_pc.agent_memory_delete.side_effect = RuntimeError("parser down")
    with patch("recall_index._get_parser_client", return_value=mock_pc):
        r = await client.delete(f"/agent/sessions/{session_id}",
                                headers={"X-User-Id": "u1"})
    assert r.status_code == 200  # session deletion never fails on this
    # Rows must be gone despite the failed vector cleanup.
    import sys
    main = sys.modules["main"]
    row = main._conn.execute(
        "SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
    assert row is None
