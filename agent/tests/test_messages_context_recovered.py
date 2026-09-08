"""/messages re-attaches context_recovered hints from event_log (spec §6.3)."""
import json
import time

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    c = main._conn

    def _clean():
        c.execute("DELETE FROM event_log WHERE run_id IN ('cr-run1','cr-run2')")
        c.execute("DELETE FROM agent_runs WHERE id IN ('cr-run1','cr-run2')")
        c.execute("DELETE FROM messages WHERE session_id='cr-s1'")
        c.execute("DELETE FROM sessions WHERE id='cr-s1'")
        c.commit()

    _clean()
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('cr-s1','cr-u1',0,0)")
    c.commit()
    yield TestClient(main.app)
    _clean()


def _history(n_turns):
    items = []
    for i in range(n_turns):
        items += [{"role": "user", "content": f"q{i}"},
                  {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"a{i}"}]}]
    return items


def _run(c, run_id, created_at, events):
    c.execute("INSERT INTO agent_runs(id, session_id, user_id, status, user_message, created_at) "
              "VALUES(?, 'cr-s1', 'cr-u1', 'done', 'q', ?)", (run_id, created_at))
    for seq, ev in enumerate(events, 1):
        c.execute("INSERT INTO event_log(run_id, seq, payload, created_at) VALUES(?,?,?,?)",
                  (run_id, seq, json.dumps(ev), created_at))
    c.commit()


def test_context_recovered_is_rehydrated_onto_the_matching_turn(client):
    runner = main.AgentRunner(main._conn)
    runner._save_history("cr-s1", _history(2))
    now = int(time.time())
    _run(main._conn, "cr-run1", now - 10, [{"type": "thinking", "content": "x"}, {"type": "done"}])
    _run(main._conn, "cr-run2", now - 5, [{"type": "context_recovered", "before": 140000, "after": 90000, "window": 117964},
                                          {"type": "message_delta", "content": "a1"}, {"type": "done"}])
    r = client.get("/agent/sessions/cr-s1/messages", headers={"X-User-Id": "cr-u1"})
    assert r.status_code == 200
    turns = [m for m in r.json() if m.get("role") == "assistant"]
    assert len(turns) == 2
    assert not any(b.get("type") == "context_recovered" for b in turns[0]["blocks"])   # run1 had none
    cards = [b for b in turns[1]["blocks"] if b.get("type") == "context_recovered"]
    assert cards == [{"type": "context_recovered", "before": 140000, "after": 90000, "window": 117964}]
    assert turns[1]["blocks"][-1]["type"] == "context_recovered"                       # appended at the end


def test_no_events_leaves_messages_untouched(client):
    runner = main.AgentRunner(main._conn)
    runner._save_history("cr-s1", _history(1))
    r = client.get("/agent/sessions/cr-s1/messages", headers={"X-User-Id": "cr-u1"})
    assert r.status_code == 200
    assert not any(b.get("type") == "context_recovered" for m in r.json() for b in m.get("blocks", []))
