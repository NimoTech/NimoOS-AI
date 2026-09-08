import json
import time

import pytest

import db as db_module
from ask import pipeline as pl
from ask import store
from tests.conftest import unfence


class Sink:
    def __init__(self):
        self.events = []

    async def put(self, ev):
        self.events.append(ev)


def _hit(fid, chunk, text):
    return {"score": 0.5, "file_id": fid, "kind": "body", "mime": "text/csv",
            "paths": [{"path": f"/D/{fid}.csv"}], "cite": {"chunk_no": chunk},
            "preview": {"text": text}, "parent_id": "", "section": ""}


class Search:
    def __init__(self, hits_by_query, fail=False):
        self.h, self.fail, self.calls = hits_by_query, fail, []

    async def search_text(self, q, *, user_id, top_k, rerank, timeout_s=None):
        self.calls.append(q)
        if self.fail:
            raise RuntimeError("down")
        return {"hits": self.h.get(q, []), "warnings": []}

    async def invoke_tool(self, name, args, user_id=None):
        if name == "read_document":
            return {"text": "FULL " + args["file_id"], "truncated": False}
        return {"chunks": []}


class Parser:
    async def notes_query(self, user_id, query, top_k=10, statuses=None):
        return {"hits": []}


def _conn():
    c = db_module.init_db(":memory:")
    c.execute("INSERT INTO sessions (id, user_id, title, created_at, updated_at, agent_type) VALUES ('s','u',NULL,1,1,'search')")
    return c


def _plan_json(*qs, needs=True):
    return json.dumps({"needs_retrieval": needs, "intent": "lookup" if needs else "chat",
                       "queries": [{"q": q, "lang": "any"} for q in qs], "answer_shape": "value"})


@pytest.mark.asyncio
async def test_happy_path_events_block_and_store():
    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json("q1", "q2")

    sink, conn = Sink(), _conn()
    search = Search({"q1": [_hit("a", 1, "alpha")], "q2": [_hit("a", 1, "alpha"), _hit("b", 2, "beta")]})
    res = await pl.run(question="What?", session_id="s", user_id="u", run_id="r1", complete=complete,
                       sink=sink, conn=conn, search=search, parser=Parser())
    types = [(e["type"], e.get("stage"), e.get("status")) for e in sink.events]
    assert types[:2] == [("ask_stage", "rewrite", "start"), ("ask_plan", None, None)]
    assert ("ask_stage", "rewrite", "done") in types and ("ask_stage", "retrieve", "done") in types
    assert ("ask_stage", "rank", "done") in types and ("ask_stage", "pack", "done") in types
    assert types[-2:] == [("ask_sources", None, None), ("ask_stage", "answer", "start")]
    plans = [e for e in sink.events if e["type"] == "ask_plan"]
    assert plans[-1]["queries"][0]["hits"] == 1 and plans[-1]["queries"][1]["hits"] == 2
    body = unfence(res.evidence_block, source="evidence")
    assert "[1] a.csv" in body and "[2] b.csv" in body and "FULL a" in body
    assert res.sources[0]["file_id"] == "a" and res.sources[0]["hit_queries"] == [0, 1]
    turns = store.list_turns(conn, "s")
    assert len(turns) == 1 and turns[0]["run_id"] == "r1" and len(turns[0]["sources"]) == 2
    assert all("ms" in st for st in res.stages)


@pytest.mark.asyncio
async def test_no_retrieval_skips_search():
    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json(needs=False)

    sink, search = Sink(), Search({})
    res = await pl.run(question="hi", session_id="s", user_id="u", run_id="r", complete=complete,
                       sink=sink, conn=_conn(), search=search, parser=Parser())
    assert search.calls == [] and res.evidence_block == "" and res.sources == []
    assert ("ask_stage", "retrieve", "skipped") in [(e["type"], e.get("stage"), e.get("status")) for e in sink.events]


@pytest.mark.asyncio
async def test_rewrite_fallback_still_retrieves():
    async def complete(instruction, body, *, max_tokens, timeout):
        return "nope"

    sink, search = Sink(), Search({"What is X?": [_hit("a", 1, "t")]})
    res = await pl.run(question="What is X?", session_id="s", user_id="u", run_id="r", complete=complete,
                       sink=sink, conn=_conn(), search=search, parser=Parser())
    assert ("ask_stage", "rewrite", "fallback") in [(e["type"], e.get("stage"), e.get("status")) for e in sink.events]
    assert "What is X?" in search.calls and res.sources


@pytest.mark.asyncio
async def test_all_queries_failed_emits_error_and_empty_block():
    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json("a", "b")

    sink = Sink()
    res = await pl.run(question="q", session_id="s", user_id="u", run_id="r", complete=complete,
                       sink=sink, conn=_conn(), search=Search({}, fail=True), parser=Parser())
    assert ("ask_stage", "retrieve", "error") in [(e["type"], e.get("stage"), e.get("status")) for e in sink.events]
    assert res.evidence_block == "" and "retrieve_failed" in res.warnings


@pytest.mark.asyncio
async def test_mece_excludes_previous_turn_chunks():
    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json("q1", "q2")

    conn = _conn()
    hits = {"q1": [_hit("a", 1, "a1"), _hit("b", 1, "b1"), _hit("c", 1, "c1"), _hit("d", 1, "d1")], "q2": []}
    await pl.run(question="first", session_id="s", user_id="u", run_id="r1", complete=complete,
                 sink=Sink(), conn=conn, search=Search(hits), parser=Parser())
    res2 = await pl.run(question="second", session_id="s", user_id="u", run_id="r2", complete=complete,
                        sink=Sink(), conn=conn, search=Search(hits), parser=Parser())
    # all four were shown in turn 1 → fewer than MECE_MIN_KEEP fresh → backfilled and flagged seen
    assert res2.sources and all(s["seen"] for s in res2.sources)


@pytest.mark.asyncio
async def test_run_guarded_never_raises():
    class Boom:
        async def search_text(self, *a, **k):
            raise RuntimeError("x")

    async def complete(*a, **k):
        raise RuntimeError("model")

    sink = Sink()
    conn = _conn()
    conn.close()   # store access will raise → must be swallowed
    res = await pl.run_guarded(question="q", session_id="s", user_id="u", run_id="r", complete=complete,
                               sink=sink, conn=conn, search=Boom(), parser=Parser())
    assert res.evidence_block == ""
    assert any(e["type"] == "ask_stage" and e["status"] == "error" for e in sink.events)
