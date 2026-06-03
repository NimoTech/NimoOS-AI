import db as db_module
import main as main_module


def _mk_conn(tmp_path, name):
    conn = db_module.init_db(str(tmp_path / name), snapshots_root=str(tmp_path / "s"))
    now = 1000
    conn.execute("INSERT INTO sessions (id,user_id,title,created_at,updated_at) "
                 "VALUES (?,?,?,?,?)", ("s1", "u1", None, now, now))
    conn.commit()
    return conn


def _add_req(conn, confirm_id, run_id, path, decision, created_at):
    conn.execute(
        "INSERT INTO access_requests "
        "(confirm_id,session_id,run_id,path,kind,reason,decision,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (confirm_id, "s1", run_id, path, "folder", "需要浏览该文件夹", decision, created_at))
    conn.commit()


def test_inject_attaches_resolved_card_to_assistant_turn(tmp_path):
    conn = _mk_conn(tmp_path, "a.db")
    _add_req(conn, "c1", "r1", "/DATA/Docs", "granted", 1001)
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "blocks": [{"type": "md", "text": "ok"}]},
    ]
    out = main_module._inject_access_request_cards(messages, "s1", conn)
    asst = [m for m in out if m.get("role") == "assistant"][0]
    cards = [b for b in asst["blocks"] if b.get("type") == "access_request"]
    assert len(cards) == 1
    c = cards[0]
    assert c["decided"] is True and c["granted"] is True
    assert c["path"] == "/DATA/Docs" and c["confirmId"] == "c1"
    assert c["kind"] == "folder" and c["reason"] == "需要浏览该文件夹"


def test_inject_denied_card_granted_false(tmp_path):
    conn = _mk_conn(tmp_path, "b.db")
    _add_req(conn, "c1", "r1", "/DATA/No", "denied", 1001)
    messages = [{"role": "assistant", "blocks": []}]
    out = main_module._inject_access_request_cards(messages, "s1", conn)
    cards = [b for m in out if m.get("role") == "assistant"
             for b in m.get("blocks", []) if b.get("type") == "access_request"]
    assert len(cards) == 1 and cards[0]["decided"] is True and cards[0]["granted"] is False


def test_inject_skips_pending_and_cancelled(tmp_path):
    conn = _mk_conn(tmp_path, "c.db")
    _add_req(conn, "c1", "r1", "/p1", None, 1001)
    _add_req(conn, "c2", "r1", "/p2", "cancelled", 1002)
    messages = [{"role": "assistant", "blocks": []}]
    out = main_module._inject_access_request_cards(messages, "s1", conn)
    cards = [b for m in out if m.get("role") == "assistant"
             for b in m.get("blocks", []) if b.get("type") == "access_request"]
    assert cards == []  # only granted/denied are shown


def test_inject_two_runs_map_to_two_turns_in_order(tmp_path):
    conn = _mk_conn(tmp_path, "d.db")
    _add_req(conn, "c1", "r1", "/first", "granted", 1001)
    _add_req(conn, "c2", "r2", "/second", "denied", 1002)
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "blocks": [{"type": "md", "text": "a1"}]},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "blocks": [{"type": "md", "text": "a2"}]},
    ]
    out = main_module._inject_access_request_cards(messages, "s1", conn)
    assts = [m for m in out if m.get("role") == "assistant"]
    c1 = [b for b in assts[0]["blocks"] if b.get("type") == "access_request"]
    c2 = [b for b in assts[1]["blocks"] if b.get("type") == "access_request"]
    assert len(c1) == 1 and c1[0]["path"] == "/first" and c1[0]["granted"] is True
    assert len(c2) == 1 and c2[0]["path"] == "/second" and c2[0]["granted"] is False


def test_inject_no_assistant_turn_creates_one(tmp_path):
    conn = _mk_conn(tmp_path, "e.db")
    _add_req(conn, "c1", "r1", "/only", "granted", 1001)
    messages = [{"role": "user", "content": "hi"}]
    out = main_module._inject_access_request_cards(messages, "s1", conn)
    cards = [b for m in out if m.get("role") == "assistant"
             for b in m.get("blocks", []) if b.get("type") == "access_request"]
    assert len(cards) == 1 and cards[0]["path"] == "/only"
