import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

import db as db_module


@pytest.fixture
def conn(tmp_path):
    c = db_module.init_db(str(tmp_path / "t.db"))
    c.execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) "
        "VALUES ('s1','u1',0,0)"
    )
    c.commit()
    return c


def _rows(conn, session_id="s1"):
    return conn.execute(
        "SELECT path, kind FROM visible_resources WHERE session_id=? "
        "ORDER BY path", (session_id,),
    ).fetchall()


# ─── grant_fs ──────────────────────────────────────────────────────────────


def test_grant_fs_inserts_valid_absolute_dirs(conn, tmp_path):
    from tasks import grants

    d1 = tmp_path / "a"
    d2 = tmp_path / "b"
    d1.mkdir()
    d2.mkdir()

    count = grants.grant_fs(conn, "s1", [str(d1), str(d2)])

    assert count == 2
    rows = _rows(conn)
    assert [r["path"] for r in rows] == sorted([str(d1), str(d2)])
    assert all(r["kind"] == "folder" for r in rows)


def test_grant_fs_is_idempotent(conn, tmp_path):
    from tasks import grants

    d1 = tmp_path / "a"
    d1.mkdir()

    first = grants.grant_fs(conn, "s1", [str(d1)])
    second = grants.grant_fs(conn, "s1", [str(d1)])

    assert first == 1
    assert second == 1  # re-granting still "counts" — the grant is in effect
    rows = _rows(conn)
    assert len(rows) == 1  # but no duplicate row was inserted


def test_grant_fs_skips_relative_path(conn, tmp_path):
    from tasks import grants

    count = grants.grant_fs(conn, "s1", ["relative/dir"])

    assert count == 0
    assert _rows(conn) == []


def test_grant_fs_skips_nonexistent_dir(conn, tmp_path):
    from tasks import grants

    missing = tmp_path / "does-not-exist"

    count = grants.grant_fs(conn, "s1", [str(missing)])

    assert count == 0
    assert _rows(conn) == []


def test_grant_fs_skips_file_not_dir(conn, tmp_path):
    from tasks import grants

    f = tmp_path / "file.txt"
    f.write_text("x")

    count = grants.grant_fs(conn, "s1", [str(f)])

    assert count == 0
    assert _rows(conn) == []


def test_grant_fs_mixed_valid_and_invalid(conn, tmp_path):
    from tasks import grants

    good = tmp_path / "good"
    good.mkdir()

    count = grants.grant_fs(
        conn, "s1", [str(good), "relative", str(tmp_path / "missing")]
    )

    assert count == 1
    assert [r["path"] for r in _rows(conn)] == [str(good)]


def test_grant_fs_empty_list(conn):
    from tasks import grants

    assert grants.grant_fs(conn, "s1", []) == 0
    assert _rows(conn) == []


# ─── grant_egress ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grant_egress_uses_bare_host_and_returns_mapping(monkeypatch):
    """The egress-proxy's consumer side (handleConnect/pumpUploadGated in
    deploy/agent/egress-proxy/main.go) does net.SplitHostPort(hostport) and
    then looks grants up by the resulting bare host — a key with a port
    attached would never match. register_grant must therefore be called with
    the plain domain, no port appended."""
    from tasks import grants

    calls = []

    def fake_register_grant(host, max_bytes, ttl_sec=60):
        calls.append((host, max_bytes, ttl_sec))
        return True

    monkeypatch.setattr(grants._egress_grant, "register_grant", fake_register_grant)

    result = await grants.grant_egress(["api.example.com", "other.example.com"])

    assert result == {"api.example.com": True, "other.example.com": True}
    hosts = [c[0] for c in calls]
    assert hosts == ["api.example.com", "other.example.com"]


@pytest.mark.asyncio
async def test_grant_egress_strips_port_if_present(monkeypatch):
    """If a preauth document's domain entry carries a port (e.g. someone
    wrote "api.example.com:443"), it must be stripped before registering —
    matching the proxy's bare-host lookup, not appended to it."""
    from tasks import grants

    calls = []
    monkeypatch.setattr(
        grants._egress_grant, "register_grant",
        lambda host, max_bytes, ttl_sec=60: calls.append(host) or True,
    )

    await grants.grant_egress(["api.example.com:8443"])

    assert calls == ["api.example.com"]


@pytest.mark.asyncio
async def test_grant_egress_uses_provided_budget_and_ttl(monkeypatch):
    from tasks import grants

    captured = {}

    def fake(host, max_bytes, ttl_sec=60):
        captured["max_bytes"] = max_bytes
        captured["ttl_sec"] = ttl_sec
        return True

    monkeypatch.setattr(grants._egress_grant, "register_grant", fake)

    await grants.grant_egress(["x.example.com"], max_bytes=123, ttl_sec=99)

    assert captured == {"max_bytes": 123, "ttl_sec": 99}


@pytest.mark.asyncio
async def test_grant_egress_false_result_is_propagated(monkeypatch):
    from tasks import grants

    monkeypatch.setattr(
        grants._egress_grant, "register_grant",
        lambda host, max_bytes, ttl_sec=60: False,
    )

    result = await grants.grant_egress(["fails.example.com"])

    assert result == {"fails.example.com": False}


@pytest.mark.asyncio
async def test_grant_egress_never_raises_on_register_grant_exception(monkeypatch):
    from tasks import grants

    def boom(host, max_bytes, ttl_sec=60):
        raise RuntimeError("boom")

    monkeypatch.setattr(grants._egress_grant, "register_grant", boom)

    result = await grants.grant_egress(["explodes.example.com"])

    assert result == {"explodes.example.com": False}


@pytest.mark.asyncio
async def test_grant_egress_empty_list(monkeypatch):
    from tasks import grants

    result = await grants.grant_egress([])

    assert result == {}


@pytest.mark.asyncio
async def test_grant_egress_uses_run_in_executor(monkeypatch):
    """register_grant is synchronous urllib and must not run on the event
    loop directly — it must go through run_in_executor."""
    from tasks import grants
    import asyncio

    loop = asyncio.get_running_loop()
    original = loop.run_in_executor
    executor_calls = []

    def spy(executor, func, *a):
        executor_calls.append((executor, func))
        return original(executor, func, *a)

    monkeypatch.setattr(loop, "run_in_executor", spy)
    monkeypatch.setattr(
        grants._egress_grant, "register_grant",
        lambda host, max_bytes, ttl_sec=60: True,
    )

    await grants.grant_egress(["exec.example.com"])

    assert len(executor_calls) == 1
