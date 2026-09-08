import pytest

from ask import evidence as ev
from ask.retrieve import Candidate
from tests.conftest import unfence


def _c(fid, chunk, text, *, start=None, end=None, parent="", rrf=1.0, source="document", mime="text/csv"):
    return Candidate(key=f"doc:{fid}:body:{chunk}", source=source, file_id=fid, kind="body", chunk_no=chunk,
                     text=text, name=f"{fid}.csv", path=f"/D/{fid}.csv", mime=mime, parent_id=parent,
                     offset_start=start, offset_end=end, rrf=rrf, hit_queries=[0], merged_chunk_nos=[chunk])


def test_stitch_dedupes_overlap_by_offsets():
    assert ev.stitch("abcdef", 6, "defghi", 3) == "abcdefghi"
    assert ev.stitch("abc", None, "def", None) == "abc\n…\ndef"
    assert ev.stitch("abc", 3, "xyz", 10) == "abc\n…\nxyz"      # gap → ellipsis


def test_merge_adjacent_keeps_rank_of_best_member():
    a1 = _c("a", 1, "AAAA", start=0, end=4, rrf=0.5)
    a2 = _c("a", 2, "AABB", start=2, end=6, rrf=0.9)
    b1 = _c("b", 1, "B", rrf=0.7)
    out = ev.merge_adjacent([a2, b1, a1])
    assert [c.key for c in out] == ["doc:a:body:1", "doc:b:body:1"]
    m = out[0]
    assert m.text == "AAAABB" and m.merged_chunk_nos == [1, 2] and m.rrf == 0.9 and m.chunk_no == 1
    assert m.raw_scores is not a2.raw_scores


def test_merge_adjacent_does_not_merge_gaps_or_other_files():
    out = ev.merge_adjacent([_c("a", 1, "x"), _c("a", 3, "y"), _c("b", 2, "z")])
    assert len(out) == 3


@pytest.mark.asyncio
async def test_expand_parents_replaces_group_with_section():
    calls = []

    async def invoke_tool(name, args, user_id):
        calls.append((name, args))
        return {"parent_id": "P", "section": "Spec", "chunks": [
            {"chunk_no": 1, "text": "one ", "offset_start": 0, "offset_end": 4},
            {"chunk_no": 2, "text": " two", "offset_start": 3, "offset_end": 7},
            {"chunk_no": 3, "text": "three", "offset_start": 7, "offset_end": 12}]}

    cands = [_c("a", 2, "two", parent="P", rrf=0.9), _c("b", 1, "solo", parent="Q", rrf=0.8),
             _c("a", 1, "one", parent="P", rrf=0.5)]
    out = await ev.expand_parents(cands, invoke_tool=invoke_tool, user_id="u", max_chars=6000)
    assert [c.key for c in out] == ["doc:a:body:2", "doc:b:body:1"]
    assert out[0].parent is True and out[0].text == "one twothree" and out[0].merged_chunk_nos == [1, 2, 3]
    assert calls == [("read_file_chunk", {"file_id": "a", "kind": "body", "chunk_no": 2, "parent": True})]


@pytest.mark.asyncio
async def test_expand_parents_truncates_and_survives_failure():
    async def invoke_tool(name, args, user_id):
        if args["file_id"] == "x":
            raise RuntimeError("no")
        return {"chunks": [{"chunk_no": 1, "text": "z" * 100}]}

    cands = [_c("x", 1, "a", parent="P"), _c("x", 2, "b", parent="P"), _c("y", 1, "c", parent="R"), _c("y", 2, "d", parent="R")]
    out = await ev.expand_parents(cands, invoke_tool=invoke_tool, user_id="u", max_chars=10)
    assert [c.key for c in out] == ["doc:x:body:1", "doc:x:body:2", "doc:y:body:1"]
    assert out[2].text == "z" * 10 + "…" and out[2].parent is True


@pytest.mark.asyncio
async def test_inline_small_docs_keeps_one_item_per_inlined_file():
    """The full text is the whole file, so it belongs on exactly one item.
    Writing it onto every chunk hit of that file shipped identical [n] entries
    and charged the evidence budget for each copy."""
    async def invoke_tool(name, args, user_id):
        return {"text": "FULL " + args["file_id"], "truncated": False}

    cands = [_c("a", 1, "part", rrf=0.5), _c("p", 1, "pdf", mime="application/pdf", rrf=0.4),
             _c("a", 2, "part2", rrf=0.9)]
    out = await ev.inline_small_docs(cands, invoke_tool=invoke_tool, user_id="u")
    # the file's best-ranked chunk carries the text, at its own position
    assert [c.key for c in out] == ["doc:p:body:1", "doc:a:body:2"]
    assert out[1].full_text is True and out[1].text == "FULL a"
    assert out[0].full_text is False and out[0].text == "pdf"   # not text/* → not inlined


@pytest.mark.asyncio
async def test_inline_small_docs_leaves_the_list_alone_when_nothing_inlines():
    async def invoke_tool(name, args, user_id):
        return {"text": "x", "truncated": True}

    cands = [_c("a", 1, "part"), _c("a", 2, "part2")]
    out = await ev.inline_small_docs(cands, invoke_tool=invoke_tool, user_id="u")
    assert [c.key for c in out] == ["doc:a:body:1", "doc:a:body:2"]
    assert all(c.full_text is False for c in out)


def test_apply_budget_by_items_and_chars():
    cands = [_c("a", 1, "x" * 50, rrf=0.9), _c("b", 1, "y" * 50, rrf=0.8), _c("c", 1, "z" * 50, rrf=0.7)]
    kept, dropped = ev.apply_budget(cands, max_items=2, budget_chars=1000)
    assert [c.file_id for c in kept] == ["a", "b"] and dropped == 1
    kept, dropped = ev.apply_budget(cands, max_items=10, budget_chars=120)
    assert [c.file_id for c in kept] == ["a", "b"] and dropped == 1
    kept, dropped = ev.apply_budget([_c("big", 1, "q" * 500)], max_items=10, budget_chars=100)
    assert len(kept) == 1 and kept[0].text == "q" * 100 + "…" and dropped == 0


def test_render_pack_numbering_and_fence():
    pack = ev.EvidencePack(items=[_c("a", 1, "alpha"), _c("b", 3, "beta")], dropped=1, total_chars=9, step_summaries=["s1"])
    pack.items[1].full_text = True
    pack.items[0].hit_queries = [0, 2]
    out = ev.render_pack(pack, budget_chars=24000)
    body = unfence(out, source="evidence")
    assert body.startswith(ev.EVIDENCE_INTRO)
    assert "[EVIDENCE START]" in body and body.strip().endswith("[EVIDENCE END]")
    assert "evidence>" not in body
    assert "[1] a.csv" in body and "/D/a.csv" in body and "chunk 1" in body and "hit by: q1, q3" in body
    assert "[2] b.csv" in body and "full text" in body
    assert "Step summaries:\n- s1" in body
    assert body.index("[1] a.csv") < body.index("alpha") < body.index("[2] b.csv") < body.index("beta")
    assert ev.render_pack(ev.EvidencePack([], 0, 0, []), budget_chars=100) == ""


def test_render_pack_omits_hit_line_for_notes():
    note = Candidate(key="note:n1:0", source="note", file_id="note:n1", kind="note", chunk_no=0,
                     text="note body", name="My note", path="notes://n1", mime="text/markdown",
                     note_id="n1", hit_queries=[], merged_chunk_nos=[0])
    pack = ev.EvidencePack(items=[note], dropped=0, total_chars=9, step_summaries=[])
    body = unfence(ev.render_pack(pack, budget_chars=24000), source="evidence")
    assert "[1] My note" in body and "knowledge note" in body
    assert "hit by" not in body


def test_sources_payload_shape():
    c = _c("a", 1, "alpha " * 100)
    c.seen = True
    pack = ev.EvidencePack(items=[c], dropped=0, total_chars=1, step_summaries=[])
    [s] = ev.sources_payload(pack, total_queries=3)
    assert s["n"] == 1 and s["source"] == "document" and s["file_id"] == "a" and s["chunk_no"] == 1
    assert s["seen"] is True and s["full_text"] is False and s["hit_queries"] == [0] and s["total_queries"] == 3
    assert len(s["snippet"]) <= 200 and s["path"] == "/D/a.csv" and s["name"] == "a.csv"


@pytest.mark.asyncio
async def test_build_pack_orchestrates_and_counts():
    async def invoke_tool(name, args, user_id):
        return {"text": "FULL", "truncated": False} if name == "read_document" else {"chunks": []}

    cands = [_c("a", 1, "x" * 10, rrf=0.9), _c("a", 2, "y" * 10, start=10, end=20, rrf=0.8), _c("b", 1, "z" * 10, rrf=0.1)]
    cands[0].offset_start, cands[0].offset_end = 0, 10
    pack = await ev.build_pack(cands, invoke_tool=invoke_tool, user_id="u", max_items=1, budget_chars=1000)
    assert len(pack.items) == 1 and pack.dropped == 1 and pack.items[0].full_text is True
    assert pack.total_chars == len(pack.items[0].text)
