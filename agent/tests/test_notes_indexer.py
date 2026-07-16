import asyncio

from notes import indexer


class _FakeParser:
    def __init__(self):
        self.calls = []

    async def notes_upsert(self, **kw):
        self.calls.append(("upsert", kw))
        return {"upserted": len(kw["chunks"])}

    async def notes_delete(self, user_id, note_id):
        self.calls.append(("delete", user_id, note_id))
        return {"ok": True}


def test_chunk_note_prefixes_title_and_splits():
    chunks = indexer.chunk_note("T", "a" * 3000, max_chars=2000)
    assert chunks[0]["chunk_no"] == 0
    assert chunks[0]["text"].startswith("# T\n")
    assert len(chunks) == 2


def test_index_note_sends_payload(monkeypatch):
    fake = _FakeParser()
    monkeypatch.setattr(indexer, "_CLIENT", fake)
    note = {"id": "n1", "user_id": "1", "type": "insight",
            "status": "draft", "created_by": "pipeline",
            "updated_at": 9, "title": "T"}
    ok = asyncio.get_event_loop().run_until_complete(
        indexer.index_note(note, "body"))
    assert ok is True
    kind, kw = fake.calls[0]
    assert kind == "upsert" and kw["user_id"] == "1"
    assert kw["note_id"] == "n1" and kw["note_type"] == "insight"
    assert kw["chunks"][0]["text"].startswith("# T\n")


def test_index_note_swallows_errors(monkeypatch):
    class _Boom:
        async def notes_upsert(self, **kw):
            raise RuntimeError("down")
    monkeypatch.setattr(indexer, "_CLIENT", _Boom())
    ok = asyncio.get_event_loop().run_until_complete(
        indexer.index_note({"id": "n", "user_id": "1", "type": "note",
                            "status": "draft", "created_by": "agent",
                            "updated_at": 0, "title": "t"}, "b"))
    assert ok is False


def test_deindex(monkeypatch):
    fake = _FakeParser()
    monkeypatch.setattr(indexer, "_CLIENT", fake)
    ok = asyncio.get_event_loop().run_until_complete(
        indexer.deindex_note("1", "n1"))
    assert ok is True and fake.calls[0] == ("delete", "1", "n1")
