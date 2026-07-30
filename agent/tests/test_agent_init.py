import os
import time
import pytest
import db as db_module
import agent as agent_module

from tests.test_agent_md_prompt import unfence_block


@pytest.fixture(autouse=True)
def _ceiling(tmp_path, monkeypatch):
    """_compose_system_prompt calls agent_md.probe() with no ceiling, so pin
    one for these tests — pytest's tmp_path lives under /tmp (mode 1777),
    which agent_md.probe correctly treats as a world-writable ancestor and
    refuses to load agent.md from underneath. See tests/test_agent_md_prompt.py
    for the same fixture applied to the newer tests."""
    real_probe = agent_module.agent_md.probe

    def probe_with_ceiling(folder, **kw):
        kw.setdefault("ceiling", str(tmp_path))
        return real_probe(folder, **kw)

    monkeypatch.setattr(agent_module.agent_md, "probe", probe_with_ceiling)


def test_compose_no_visible_resources(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                              snapshots_root=str(tmp_path / "snap"))
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, 0, 0))
    conn.commit()
    out = agent_module._compose_system_prompt(conn, "s1", "BASE")
    assert "No filesystem resources" in out


def test_compose_includes_agent_md(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                              snapshots_root=str(tmp_path / "snap"))
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, 0, 0))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "agent.md").write_text("# This project is foo")
    conn.execute("INSERT INTO visible_resources (session_id, path, kind, added_at) "
                 "VALUES (?,?,?,?)", ("s1", str(root), "folder", 0))
    conn.commit()
    out = agent_module._compose_system_prompt(conn, "s1", "BASE")
    assert "has agent.md" in out
    body = unfence_block(out, f"agent-md:{os.path.join(str(root), 'agent.md')}")
    assert "# This project is foo" in body


def test_compose_truncates_at_total_cap(tmp_path):
    conn = db_module.init_db(str(tmp_path / "a.db"),
                              snapshots_root=str(tmp_path / "snap"))
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, 0, 0))
    huge = "x" * (40 * 1024)
    for i in range(2):
        d = tmp_path / f"p{i}"
        d.mkdir()
        (d / "agent.md").write_text(huge)
        conn.execute("INSERT INTO visible_resources "
                     "(session_id, path, kind, added_at) VALUES (?,?,?,?)",
                     ("s1", str(d), "folder", i))
    conn.commit()
    out = agent_module._compose_system_prompt(conn, "s1", "BASE",
                                               max_per_file=8 * 1024,
                                               max_total=8 * 1024)
    assert "truncated" in out


import agent as agent_module


def test_system_prompt_allows_attempting_and_stops_on_deny():
    p = agent_module.SYSTEM_PROMPT
    assert "授权" in p or "access" in p.lower()      # mentions auto access-request
    assert "停止" in p or "stop" in p.lower()         # deny-stop instruction
