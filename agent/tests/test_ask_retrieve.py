import asyncio
import time

import pytest

from ask import retrieve as rt
from ask.rewrite import Plan, SubQuery


def _hit(fid, chunk, text="t", score=0.5, **extra):
    h = {"score": score, "file_id": fid, "kind": "body", "mime": "text/csv",
         "paths": [{"root_id": "r", "path": f"/Documents/{fid}.csv", "mtime_ms": 1}],
         "cite": {"chunk_no": chunk, "offset_start": chunk * 100, "offset_end": chunk * 100 + 120, "page": None},
         "preview": {"text": text}, "parent_id": f"p-{fid}", "section": "Spec"}
    h.update(extra)
    return h


def test_candidate_from_hit_maps_fields():
    c = rt.candidate_from_hit(_hit("f1", 2, "hello"))
    assert c.key == "doc:f1:body:2" and c.source == "document"
    assert c.name == "f1.csv" and c.path == "/Documents/f1.csv"
    assert c.offset_start == 200 and c.offset_end == 320 and c.section == "Spec"
    assert rt.candidate_from_hit({"file_id": "", "preview": {"text": "x"}}) is None
    assert rt.candidate_from_hit(_hit("f2", 0, "")) is None


def test_candidate_from_note():
    c = rt.candidate_from_note({"note_id": "n1", "chunk_no": 0, "text": "body", "type": "note",
                                "status": "curated", "score": 0.9}, title="My note")
    assert c.key == "note:n1:0" and c.source == "note" and c.name == "My note"
    assert c.path == "notes://n1" and c.kind == "note" and c.file_id == "note:n1"


def test_rrf_fuse_math_and_hit_queries():
    a1, a2, b1 = (rt.candidate_from_hit(_hit("a", 1)), rt.candidate_from_hit(_hit("a", 2)),
                  rt.candidate_from_hit(_hit("b", 1)))
    a1_again = rt.candidate_from_hit(_hit("a", 1))
    fused = rt.rrf_fuse([[a1, b1], [a1_again, a2]], k=60)
    keys = [c.key for c in fused]
    assert keys[0] == "doc:a:body:1"
    top = fused[0]
    assert top.hit_queries == [0, 1]
    assert abs(top.rrf - (1 / 61 + 1 / 61)) < 1e-9
    # b (doc:b:body:1) and a2 (doc:a:body:2) are each the sole second-place hit in
    # their own 2-item list (no weights), so both are tied at 1/(60+1+1) = 1/62.
    assert abs(fused[1].rrf - 1 / 62) < 1e-9 and abs(fused[2].rrf - 1 / 62) < 1e-9


def test_rrf_fuse_weights_notes():
    d = rt.candidate_from_hit(_hit("a", 1))
    n = rt.candidate_from_note({"note_id": "n", "chunk_no": 0, "text": "x", "score": 1})
    fused = rt.rrf_fuse([[d], [n]], k=60, weights=[1.0, 1.2])
    assert fused[0].key == "note:n:0" and abs(fused[0].rrf - 1.2 / 61) < 1e-9


def test_apply_mece_excludes_seen_but_backfills_below_min():
    cs = [rt.candidate_from_hit(_hit(f, 1)) for f in "abcde"]
    out = rt.apply_mece(cs, {"doc:a:body:1", "doc:b:body:1"}, min_keep=3)
    assert [c.key for c in out] == ["doc:c:body:1", "doc:d:body:1", "doc:e:body:1"]
    out2 = rt.apply_mece(cs, {f"doc:{f}:body:1" for f in "abcd"}, min_keep=3)
    assert [c.key for c in out2] == ["doc:e:body:1", "doc:a:body:1", "doc:b:body:1"]
    assert out2[0].seen is False and out2[1].seen is True and out2[2].seen is True


class _Search:
    def __init__(self, results, fail=(), slow=()):
        self.results, self.fail, self.slow, self.calls = results, set(fail), set(slow), []

    async def search_text(self, query, *, user_id, top_k, rerank, timeout_s=None):
        self.calls.append((query, user_id, top_k, rerank))
        if query in self.slow:
            await asyncio.sleep(5)
        if query in self.fail:
            raise RuntimeError("search down")
        return {"hits": self.results.get(query, []), "warnings": ["rerank_unavailable"] if query == "w" else []}


class _Parser:
    def __init__(self, hits=None, fail=False):
        self.hits, self.fail, self.calls = hits or [], fail, []

    async def notes_query(self, user_id, query, top_k=10, statuses=None):
        self.calls.append((user_id, query, top_k, statuses))
        if self.fail:
            raise RuntimeError("parser down")
        return {"hits": self.hits}


def _plan(*qs):
    return Plan(True, "lookup", tuple(SubQuery(q) for q in qs), "prose")


@pytest.mark.asyncio
async def test_retrieve_fans_out_and_fuses():
    search = _Search({"q1": [_hit("a", 1), _hit("b", 1)], "w": [_hit("a", 1)]})
    parser = _Parser(hits=[{"note_id": "n1", "chunk_no": 0, "text": "note text", "score": 0.7}])
    res = await rt.retrieve(_plan("q1", "w"), question="orig", user_id="u", search=search, parser=parser,
                            deadline=time.monotonic() + 10, note_title=lambda nid: "T")
    assert res.all_failed is False and res.partial is False
    assert res.per_query_hits == [2, 1]
    assert res.candidates[0].key == "doc:a:body:1" and res.candidates[0].hit_queries == [0, 1]
    assert any(c.source == "note" and c.name == "T" for c in res.candidates)
    assert "rerank_unavailable" in res.warnings
    assert parser.calls[0][3] == ["curated"] and parser.calls[0][1] == "orig"
    assert all(call[2] == rt.config.SEARCH_TOP_K and call[3] is True for call in search.calls)


@pytest.mark.asyncio
async def test_retrieve_one_failure_is_a_warning_all_failures_flag():
    search = _Search({"ok": [_hit("a", 1)]}, fail={"bad"})
    res = await rt.retrieve(_plan("ok", "bad"), question="q", user_id="u", search=search, parser=_Parser(fail=True),
                            deadline=time.monotonic() + 10)
    assert res.all_failed is False and len(res.candidates) == 1
    assert any(w.startswith("query_failed:") for w in res.warnings) and "notes_unavailable" in res.warnings
    res2 = await rt.retrieve(_plan("bad", "bad2"), question="q", user_id="u",
                             search=_Search({}, fail={"bad", "bad2"}), parser=_Parser(), deadline=time.monotonic() + 10)
    assert res2.all_failed is True and res2.candidates == []


@pytest.mark.asyncio
async def test_retrieve_deadline_returns_partial():
    search = _Search({"fast": [_hit("a", 1)], "slow": [_hit("b", 1)]}, slow={"slow"})
    t0 = time.monotonic()
    res = await rt.retrieve(_plan("fast", "slow"), question="q", user_id="u", search=search, parser=_Parser(),
                            deadline=time.monotonic() + 0.3)
    assert time.monotonic() - t0 < 2
    assert res.partial is True and [c.key for c in res.candidates] == ["doc:a:body:1"]
    assert res.per_query_hits == [1, 0]


@pytest.mark.asyncio
async def test_retrieve_draft_notes_opt_in():
    parser = _Parser()
    await rt.retrieve(_plan("a", "b"), question="q", user_id="u", search=_Search({}), parser=parser,
                      deadline=time.monotonic() + 5, include_draft_notes=True)
    assert parser.calls[0][3] == ["curated", "draft"]
