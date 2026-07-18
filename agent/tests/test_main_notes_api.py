import pytest
from httpx import AsyncClient, ASGITransport

import main as main_module
from notes import store


@pytest.fixture()
def app_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    conn = main_module._db()
    store.set_notes_root(conn, str(tmp_path / "Notes"))

    async def _fake_index(note, body):
        return True
    import main as m
    monkeypatch.setattr(m, "notes_index_note", _fake_index, raising=False)
    from notes import indexer
    monkeypatch.setattr(indexer, "_CLIENT", type("F", (), {
        "notes_upsert": staticmethod(lambda **kw: _noop()),
        "notes_delete": staticmethod(lambda u, n: _noop())})())
    return conn


async def _noop():
    return {}


def _client():
    return AsyncClient(transport=ASGITransport(app=main_module.app),
                       base_url="http://test")


@pytest.mark.asyncio
async def test_create_list_get_roundtrip(app_ctx):
    async with _client() as ac:
        r = await ac.post("/agent/notes", headers={"X-User-Id": "1"},
                          json={"title": "T", "content": "body",
                                "tags": ["x"]})
        assert r.status_code == 201
        nid = r.json()["id"]
        assert r.json()["status"] == "curated"
        r2 = await ac.get("/agent/notes", headers={"X-User-Id": "1"})
        assert [n["id"] for n in r2.json()["notes"]] == [nid]
        r3 = await ac.get(f"/agent/notes/{nid}", headers={"X-User-Id": "1"})
        # notes/okf.serialize_note_text always newline-terminates the body
        # on disk (see notes/store.py module docstring + _hash); existing
        # store tests (test_notes_store.py) already compare via .strip().
        assert r3.json()["body"].strip() == "body"


@pytest.mark.asyncio
async def test_requires_user_header(app_ctx):
    async with _client() as ac:
        assert (await ac.get("/agent/notes")).status_code == 401


@pytest.mark.asyncio
async def test_cross_user_404(app_ctx):
    conn = app_ctx
    n = store.create_note(conn, "1", title="t", body="b")
    async with _client() as ac:
        r = await ac.get(f"/agent/notes/{n['id']}",
                         headers={"X-User-Id": "2"})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_put_revision_conflict_409(app_ctx):
    conn = app_ctx
    n = store.create_note(conn, "1", title="t", body="b")
    async with _client() as ac:
        r = await ac.put(f"/agent/notes/{n['id']}",
                         headers={"X-User-Id": "1"},
                         json={"expected_revision": 99, "content": "x"})
        assert r.status_code == 409
        assert r.json()["current_revision"] == 1


@pytest.mark.asyncio
async def test_curate_and_archive(app_ctx):
    conn = app_ctx
    n = store.create_note(conn, "1", title="t", body="b",
                          created_by="pipeline")     # draft
    async with _client() as ac:
        r = await ac.post(f"/agent/notes/{n['id']}/curate",
                          headers={"X-User-Id": "1"})
        assert r.json()["status"] == "curated"
        r2 = await ac.post(f"/agent/notes/{n['id']}/archive",
                           headers={"X-User-Id": "1"})
        assert r2.json()["status"] == "archived"


@pytest.mark.asyncio
async def test_settings_roundtrip_and_delete(app_ctx, tmp_path):
    conn = app_ctx
    n = store.create_note(conn, "1", title="t", body="b")
    async with _client() as ac:
        s = await ac.get("/agent/notes/settings", headers={"X-User-Id": "1"})
        assert s.json()["notes_root"].endswith("Notes")
        d = await ac.delete(f"/agent/notes/{n['id']}",
                            headers={"X-User-Id": "1"})
        assert d.json()["status"] == "deleted"
        r2 = await ac.get("/agent/notes", headers={"X-User-Id": "1"})
        assert r2.json()["notes"] == []


@pytest.mark.asyncio
async def test_settings_bad_mode_without_notes_root_rejected(app_ctx):
    async with _client() as ac:
        r = await ac.put("/agent/notes/settings", headers={"X-User-Id": "1"},
                         json={"mode": "bogus"})
    assert r.status_code == 400
    assert "mode must be adopt|migrate" in r.json()["detail"]


@pytest.mark.asyncio
async def test_settings_auto_extract_only_still_ok_with_default_mode(app_ctx):
    async with _client() as ac:
        r = await ac.put("/agent/notes/settings", headers={"X-User-Id": "1"},
                         json={"auto_extract": False})
    assert r.status_code == 200
    assert r.json()["auto_extract"] is False


@pytest.mark.asyncio
async def test_settings_migrate_refuses_nonempty_target(app_ctx, tmp_path):
    target = tmp_path / "NewNotes"
    (target / "1").mkdir(parents=True)
    (target / "1" / "existing.md").write_text("x")
    async with _client() as ac:
        r = await ac.put("/agent/notes/settings", headers={"X-User-Id": "1"},
                         json={"notes_root": str(target), "mode": "migrate"})
    assert r.status_code == 400
    assert "not empty" in r.json()["detail"]


@pytest.mark.asyncio
async def test_dir_info_probe(app_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_NOTES_PROBE_ROOT", str(tmp_path))
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    full_dir = tmp_path / "full"
    full_dir.mkdir()
    (full_dir / ".hidden").write_text("x")  # dotfiles count, like the migrate guard

    async with _client() as ac:
        r = await ac.get("/agent/notes/dir-info", headers={"X-User-Id": "1"},
                         params={"path": str(empty_dir)})
        assert r.status_code == 200
        assert r.json() == {"exists": True, "empty": True}

        r = await ac.get("/agent/notes/dir-info", headers={"X-User-Id": "1"},
                         params={"path": str(full_dir)})
        assert r.json() == {"exists": True, "empty": False}

        # Missing dir is migratable — migrate mkdirs it.
        r = await ac.get("/agent/notes/dir-info", headers={"X-User-Id": "1"},
                         params={"path": str(tmp_path / "nope")})
        assert r.json() == {"exists": False, "empty": True}

        # Outside the probe root → 400; no header → 401.
        r = await ac.get("/agent/notes/dir-info", headers={"X-User-Id": "1"},
                         params={"path": "/etc"})
        assert r.status_code == 400
        r = await ac.get("/agent/notes/dir-info", params={"path": str(empty_dir)})
        assert r.status_code == 401

        # Not captured by the /agent/notes/{note_id} route.
        r = await ac.get("/agent/notes/dir-info", headers={"X-User-Id": "1"},
                         params={"path": str(empty_dir)})
        assert "exists" in r.json()
