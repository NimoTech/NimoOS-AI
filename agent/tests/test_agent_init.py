import os
import time
import pytest
import db as db_module
import agent as agent_module


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
    assert "# This project is foo" in out


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
