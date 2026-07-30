import os

import pytest

import main as main_module


def _mk_agent_md(folder, *, dir_mode=0o755):
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, "agent.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# Purpose\nnotes\n")
    os.chmod(p, 0o644)
    os.chmod(folder, dir_mode)


@pytest.fixture(autouse=True)
def _ceiling(tmp_path, monkeypatch):
    import agent_md
    real_probe = agent_md.probe
    calls = []

    def probe_with_ceiling(folder, **kw):
        calls.append(dict(kw))
        kw.setdefault("ceiling", str(tmp_path))
        return real_probe(folder, **kw)

    monkeypatch.setattr(main_module.agent_md, "probe", probe_with_ceiling)
    return calls


@pytest.fixture
def session(tmp_path):
    conn = main_module._conn
    conn.execute("DELETE FROM visible_resources")
    conn.execute("INSERT OR REPLACE INTO sessions (id,user_id,created_at,"
                 "updated_at) VALUES ('s1','u1',0,0)")
    conn.commit()
    return conn


def _add(conn, path, kind="folder"):
    conn.execute("INSERT INTO visible_resources (session_id,path,kind,added_at)"
                 " VALUES ('s1',?,?,0)", (path, kind))
    conn.commit()


@pytest.mark.asyncio
async def test_folder_row_reports_loaded(session, tmp_path):
    folder = str(tmp_path / "proj")
    _mk_agent_md(folder)
    _add(session, folder)
    rows = await main_module.list_visible_resources("s1", x_user_id="u1")
    assert rows[0]["agent_md"] == {"state": "loaded", "reason": None,
                                   "detail": None}


@pytest.mark.asyncio
async def test_folder_row_reports_skip_reason(session, tmp_path):
    folder = str(tmp_path / "shared")
    _mk_agent_md(folder, dir_mode=0o777)
    _add(session, folder)
    rows = await main_module.list_visible_resources("s1", x_user_id="u1")
    assert rows[0]["agent_md"]["state"] == "skipped"
    assert rows[0]["agent_md"]["reason"] == "writable_parent"
    assert rows[0]["agent_md"]["detail"] == os.path.realpath(folder)


@pytest.mark.asyncio
async def test_folder_without_agent_md_is_absent(session, tmp_path):
    folder = str(tmp_path / "plain")
    os.makedirs(folder)
    _add(session, folder)
    rows = await main_module.list_visible_resources("s1", x_user_id="u1")
    assert rows[0]["agent_md"]["state"] == "absent"


@pytest.mark.asyncio
async def test_file_row_has_no_agent_md_field(session, tmp_path):
    f = tmp_path / "one.txt"
    f.write_text("x", encoding="utf-8")
    _add(session, str(f), kind="file")
    rows = await main_module.list_visible_resources("s1", x_user_id="u1")
    assert "agent_md" not in rows[0]


@pytest.mark.asyncio
async def test_probe_called_with_read_body_false(session, tmp_path, _ceiling):
    folder = str(tmp_path / "proj")
    _mk_agent_md(folder)
    _add(session, folder)
    await main_module.list_visible_resources("s1", x_user_id="u1")
    assert len(_ceiling) == 1
    assert _ceiling[0].get("read_body") is False


@pytest.mark.asyncio
async def test_probe_called_with_no_ceiling(session, tmp_path, _ceiling):
    """Production must never pass ceiling: it exists only so tests can stop
    the ancestor walk before /tmp. If list_visible_resources ever passed
    ceiling=r["path"] (collapsing the walk to the folder itself), this would
    silently reopen the writable-grandparent case (D3) the walk exists to
    catch."""
    folder = str(tmp_path / "proj")
    _mk_agent_md(folder)
    _add(session, folder)
    await main_module.list_visible_resources("s1", x_user_id="u1")
    assert len(_ceiling) == 1
    assert "ceiling" not in _ceiling[0]
