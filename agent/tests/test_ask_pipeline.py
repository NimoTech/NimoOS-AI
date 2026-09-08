import asyncio
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
    assert res.sources == [] and "retrieve_failed" in res.warnings
    # no passages, but the model is told what was attempted (spec §5 row 7)
    assert "Reason: retrieve_error." in res.evidence_block


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
    assert res.sources == [] and "[EVIDENCE START]" not in res.evidence_block
    assert any(e["type"] == "ask_stage" and e["status"] == "error" for e in sink.events)


def _big_hits(fid_prefix, n, chars):
    return [_hit(f"{fid_prefix}{i}", i, "x" * chars) for i in range(n)]


@pytest.mark.asyncio
async def test_step_summaries_gate_uses_pre_budget_pool(monkeypatch):
    """auto mode must gate on the candidate pool BEFORE the budget cut.
    pack.total_chars is post-budget and therefore always <= budget, so the old
    gate turned summaries on for every list/compare/aggregate question."""
    monkeypatch.setenv("NIMOOS_ASK_STEP_SUMMARY", "auto")
    calls = []

    async def complete(instruction, body, *, max_tokens, timeout):
        calls.append(instruction)
        return json.dumps({"needs_retrieval": True, "intent": "list", "answer_shape": "list",
                           "queries": [{"q": "q1"}, {"q": "q2"}]})

    # more candidates than EVIDENCE_MAX_ITEMS (so pack.dropped > 0, which used
    # to flip the gate on) but a tiny total pool
    search = Search({"q1": [_hit(f"f{i}", 1, "small") for i in range(12)], "q2": []})
    res = await pl.run(question="list them", session_id="s", user_id="u", run_id="r", complete=complete,
                       sink=Sink(), conn=_conn(), search=search, parser=Parser())
    assert res.sources and res.dropped > 0  # the pack was built and did drop items
    assert len(calls) == 1                  # rewrite only, no summary calls


@pytest.mark.asyncio
async def test_step_summaries_run_in_parallel_and_survive_one_timeout(monkeypatch):
    """A hung summary must cost at most its own per-call timeout and must not
    take the finished pack with it (C2: serial 10s calls used to push past
    run_guarded's wait_for and discard everything)."""
    monkeypatch.setenv("NIMOOS_ASK_STEP_SUMMARY", "always")
    monkeypatch.setattr(pl.config, "STEP_SUMMARY_TIMEOUT_S", 0.2)

    async def complete(instruction, body, *, max_tokens, timeout):
        if instruction.startswith("You plan the retrieval"):
            return _plan_json("q1", "q2")
        if "q1" in body:
            await asyncio.sleep(30)         # never returns
        return "q2 says beta [2]"

    sink, conn = Sink(), _conn()
    search = Search({"q1": [_hit("a", 1, "alpha")], "q2": [_hit("b", 1, "beta")]})
    t0 = time.monotonic()
    res = await pl.run_guarded(question="q", session_id="s", user_id="u", run_id="r1",
                               complete=complete, sink=sink, conn=conn, search=search, parser=Parser())
    assert time.monotonic() - t0 < 5        # parallel + bounded, not 30s
    body = unfence(res.evidence_block, source="evidence")
    assert "[EVIDENCE END]" in body
    assert "q2 says beta" in body and "q1:" not in body
    assert [e for e in sink.events if e["type"] == "ask_sources"][-1]["items"]
    assert len(store.list_turns(conn, "s")) == 1


@pytest.mark.asyncio
async def test_step_summaries_skipped_when_the_deadline_is_near(monkeypatch):
    """Under 8s of pipeline budget left, summaries are not started at all —
    the pack, the ask_sources event and the ask_turns row still ship."""
    monkeypatch.setenv("NIMOOS_ASK_STEP_SUMMARY", "always")
    monkeypatch.setattr(pl.config, "PIPELINE_TIMEOUT_S", 6.0)
    calls = []

    async def complete(instruction, body, *, max_tokens, timeout):
        calls.append(instruction)
        return _plan_json("q1", "q2")

    sink, conn = Sink(), _conn()
    search = Search({"q1": [_hit("a", 1, "alpha")], "q2": [_hit("b", 1, "beta")]})
    res = await pl.run(question="q", session_id="s", user_id="u", run_id="r1", complete=complete,
                       sink=sink, conn=conn, search=search, parser=Parser())
    assert len(calls) == 1                  # rewrite only
    assert "[EVIDENCE END]" in unfence(res.evidence_block, source="evidence")
    assert [e for e in sink.events if e["type"] == "ask_sources"][-1]["items"]
    assert len(store.list_turns(conn, "s")) == 1


@pytest.mark.asyncio
async def test_no_hits_tells_the_model_what_was_tried():
    """spec §5 row 7: an empty pack must still reach the model as a short
    server-authored note, or the prompt's "say which queries were tried"
    instruction has nothing to work from."""
    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json("265K turbo", "265K 睿频")

    res = await pl.run(question="q", session_id="s", user_id="u", run_id="r", complete=complete,
                       sink=Sink(), conn=_conn(), search=Search({}), parser=Parser())
    assert res.sources == []
    assert "Server-side retrieval ran 2 queries" in res.evidence_block
    assert "1) 265K turbo 2) 265K 睿频" in res.evidence_block
    assert "Reason: no_hits." in res.evidence_block
    assert "[EVIDENCE START]" not in res.evidence_block   # server-authored, not fenced data


@pytest.mark.asyncio
async def test_all_failed_note_reports_retrieve_error():
    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json("a", "b")

    sink = Sink()
    res = await pl.run(question="q", session_id="s", user_id="u", run_id="r", complete=complete,
                       sink=sink, conn=_conn(), search=Search({}, fail=True), parser=Parser())
    assert "Server-side retrieval ran 2 queries" in res.evidence_block
    assert "1) a 2) b" in res.evidence_block and "Reason: retrieve_error." in res.evidence_block
    stages = [(e.get("stage"), e.get("status")) for e in sink.events if e["type"] == "ask_stage"]
    assert ("rank", "skipped") in stages and ("pack", "skipped") in stages


@pytest.mark.asyncio
async def test_partial_retrieval_note_reports_partial_timeout(monkeypatch):
    """One query answered (with nothing), one was still running at the
    deadline: not "all failed", but the empty pack is a timeout artefact and
    the model must be told so rather than that the documents lack the answer."""
    class SlowSecond(Search):
        async def search_text(self, q, *, user_id, top_k, rerank, timeout_s=None):
            self.calls.append(q)
            if q == "b":
                await asyncio.sleep(30)
            return {"hits": [], "warnings": []}

    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json("a", "b")

    monkeypatch.setattr(pl.config, "PER_QUERY_TIMEOUT_S", 0.2)
    sink, conn = Sink(), _conn()
    res = await pl.run(question="q", session_id="s", user_id="u", run_id="r", complete=complete,
                       sink=sink, conn=conn, search=SlowSecond({}), parser=Parser())
    assert "Reason: partial_timeout." in res.evidence_block
    assert "1) a 2) b" in res.evidence_block
    assert "retrieve_partial" in res.warnings
    assert len(store.list_turns(conn, "s")) == 1


@pytest.mark.asyncio
async def test_pack_stages_only_process_the_top_slice():
    """I6: merging, parent expansion and inlining used to run over the whole
    fused pool — one read_file_chunk round trip per parent group, however deep
    in the ranking it sat."""
    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json("q1", "q2")

    hits = []
    for i in range(15):
        for chunk in (1, 3):        # non-adjacent, same parent → a parent group
            h = _hit(f"f{i:02d}", chunk, f"text {i}-{chunk}")
            h["parent_id"] = f"p{i:02d}"
            h["score"] = 1.0 - i / 100
            hits.append(h)

    calls = []

    class ParentSearch(Search):
        async def invoke_tool(self, name, args, user_id=None):
            calls.append((name, args["file_id"]))
            if name == "read_file_chunk":
                return {"chunks": [{"chunk_no": 1, "text": "sec"}]}
            return {"text": "FULL", "truncated": False}

    res = await pl.run(question="q", session_id="s", user_id="u", run_id="r", complete=complete,
                       sink=Sink(), conn=_conn(), search=ParentSearch({"q1": hits, "q2": []}),
                       parser=Parser())
    touched = sorted({fid for _, fid in calls})
    assert len(touched) == pl.config.EVIDENCE_MAX_ITEMS      # 20 candidates -> 10 parent groups
    assert touched == [f"f{i:02d}" for i in range(pl.config.EVIDENCE_MAX_ITEMS)]
    # the 10 candidates sliced off before packing are still reported as dropped
    assert res.dropped == 10


@pytest.mark.asyncio
async def test_persisted_stages_include_the_answer_stage():
    async def complete(instruction, body, *, max_tokens, timeout):
        return _plan_json("q1", "q2")

    conn = _conn()
    await pl.run(question="q", session_id="s", user_id="u", run_id="r", complete=complete,
                 sink=Sink(), conn=conn, search=Search({"q1": [_hit("a", 1, "alpha")]}), parser=Parser())
    stages = store.list_turns(conn, "s")[0]["stages"]
    assert ("answer", "start") in [(st["stage"], st["status"]) for st in stages]
    assert all("ms" in st and "detail" in st for st in stages)


@pytest.mark.asyncio
async def test_run_guarded_error_branch_resolves_every_stage(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(pl, "run", boom)
    sink = Sink()
    res = await pl.run_guarded(question="q", session_id="s", user_id="u", run_id="r", complete=None,
                               sink=sink, conn=_conn(), search=Search({}), parser=Parser())
    stages = [(e.get("stage"), e.get("status")) for e in sink.events if e["type"] == "ask_stage"]
    assert stages == [("retrieve", "error"), ("rank", "skipped"), ("pack", "skipped")]
    assert res.warnings == ["pipeline_error"]
