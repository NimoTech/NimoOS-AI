import sqlite3

import db as db_module
from ask import store


def _conn():
    return db_module.init_db(":memory:")


def _src(fid, chunk, merged=None, source="document", note_id=""):
    return {"source": source, "file_id": fid, "kind": "body" if source == "document" else "note",
            "chunk_no": chunk, "merged_chunk_nos": merged or [chunk], "note_id": note_id}


def test_insert_and_list_roundtrip():
    conn = _conn()
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at, agent_type) VALUES ('s1','u',NULL,1,1,'search')")
    tid = store.insert_turn(conn, session_id="s1", run_id="r1", question="q?",
                            plan={"intent": "lookup", "queries": [{"q": "q", "lang": "any"}]},
                            sources=[_src("a", 1)], stages=[{"stage": "rewrite", "status": "done", "ms": 3}])
    rows = store.list_turns(conn, "s1")
    assert len(rows) == 1 and rows[0]["id"] == tid and rows[0]["run_id"] == "r1"
    assert rows[0]["plan"]["intent"] == "lookup" and rows[0]["sources"][0]["file_id"] == "a"
    assert rows[0]["stages"][0]["ms"] == 3 and isinstance(rows[0]["created_at"], int)


def test_seen_keys_expands_merged_and_notes():
    conn = _conn()
    store.insert_turn(conn, session_id="s", run_id="r", question="q", plan={},
                      sources=[_src("a", 1, merged=[1, 2, 3]), _src("n", 0, source="note", note_id="n9")], stages=[])
    assert store.seen_keys(conn, "s") == {"doc:a:body:1", "doc:a:body:2", "doc:a:body:3", "note:n9:0"}
    assert store.seen_keys(conn, "other") == set()


def test_recent_questions_orders_and_limits():
    conn = _conn()
    for i in range(4):
        store.insert_turn(conn, session_id="s", run_id=f"r{i}", question=f"q{i}", plan={}, sources=[], stages=[])
        conn.execute("UPDATE ask_turns SET created_at = ? WHERE run_id = ?", (100 + i, f"r{i}"))
    assert store.recent_questions(conn, "s", n=2) == "Q1: q2\nQ2: q3"
    assert store.recent_questions(conn, "none") == ""


def test_session_purge_removes_ask_turns(monkeypatch):
    import asyncio
    import session_purge
    conn = _conn()
    conn.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at, agent_type) VALUES ('s','u',NULL,1,1,'search')")
    store.insert_turn(conn, session_id="s", run_id="r", question="q", plan={}, sources=[], stages=[])

    async def no_vectors(user_id, session_id):
        return None

    asyncio.run(session_purge.purge_session(conn, "u", "s", snapshots_root="/tmp/nonexistent-snaps",
                                            vector_cleanup=no_vectors))
    assert conn.execute("SELECT COUNT(*) FROM ask_turns").fetchone()[0] == 0
