import pytest

from skills.search import inline


def test_inline_eligible_only_small_text_body():
    assert inline.inline_eligible("text/csv", "body") is True
    assert inline.inline_eligible("application/pdf", "body") is False
    assert inline.inline_eligible("text/plain", "caption") is False


@pytest.mark.asyncio
async def test_fetch_small_documents_applies_three_budgets():
    texts = {"a": "x" * 7000, "b": "y" * 7000, "c": "z" * 3000, "d": "w" * 100}

    async def invoke_tool(name, args, user_id):
        assert name == "read_document" and user_id == "u1"
        assert args == {"file_id": args["file_id"], "offset": 0, "max_chars": inline.INLINE_MAX_DOC_CHARS}
        return {"text": texts[args["file_id"]], "truncated": False}

    out = await inline.fetch_small_documents(["a", "b", "c", "d"], invoke_tool=invoke_tool, user_id="u1")
    # a(7000)+b(7000)=14000 fit; c(3000) would exceed 16000 → skipped; d is the 4th doc → never requested
    assert set(out) == {"a", "b"}


@pytest.mark.asyncio
async def test_fetch_small_documents_skips_truncated_and_failures():
    async def invoke_tool(name, args, user_id):
        if args["file_id"] == "t":
            return {"text": "partial", "truncated": True}
        if args["file_id"] == "e":
            raise RuntimeError("boom")
        return {"text": "ok", "truncated": False}

    out = await inline.fetch_small_documents(["t", "e", "g"], invoke_tool=invoke_tool, user_id="u")
    assert out == {"g": "ok"}
