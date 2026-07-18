import json
import sqlite3
import audit as A
import db as dbmod
from confirm import ConfirmManager


def test_confirm_resolution_audited(tmp_path):
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    conn = dbmod.init_db(":memory:")
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES ('s1','u1',0,0)")
    conn.commit()
    mgr = ConfirmManager(conn)
    cid = mgr.register("s1", "shell_command", "run rm", "rm -rf /x")
    mgr.resolve(cid, True, expected_session_id="s1")
    recs = [json.loads(l) for l in (tmp_path/"audit.log").read_text().splitlines()]
    ev = [r for r in recs if r["event"] == "confirm_resolved"]
    assert ev and ev[-1]["action"] == "shell_command"
    assert ev[-1]["command"] == "rm -rf /x"
    assert ev[-1]["decision"] == "approved"
