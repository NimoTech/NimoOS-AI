import json

import agent as agent_mod
from db import init_db


def _runner(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
                 "VALUES('s1','u1',0,0)")
    conn.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) "
                 "VALUES('s2','u1',0,0)")
    conn.commit()
    r = object.__new__(agent_mod.AgentRunner)
    r._conn = conn
    return r, conn


def test_save_history_prunes_to_keep_window(tmp_path):
    r, conn = _runner(tmp_path)
    for i in range(agent_mod.SNAPSHOT_KEEP + 2):
        r._save_history("s1", [{"role": "user", "content": f"turn {i}"}])
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='s1' "
                     "AND role='history'").fetchone()[0]
    assert n == agent_mod.SNAPSHOT_KEEP
    latest = conn.execute(
        "SELECT content FROM messages WHERE session_id='s1' "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()[0]
    assert json.loads(latest)[0]["content"] == f"turn {agent_mod.SNAPSHOT_KEEP + 1}"


def test_prune_scoped_to_session(tmp_path):
    r, conn = _runner(tmp_path)
    r._save_history("s2", [{"role": "user", "content": "keep me"}])
    for i in range(agent_mod.SNAPSHOT_KEEP + 5):
        r._save_history("s1", [{"role": "user", "content": f"t{i}"}])
    n2 = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='s2'").fetchone()[0]
    assert n2 == 1
