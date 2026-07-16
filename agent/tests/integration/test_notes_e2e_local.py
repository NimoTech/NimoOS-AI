"""E2E (local): tool write → file on disk → external edit → sync pass →
reindex payload correctness. Parser is faked; everything else is real."""
import asyncio
import json

import pytest

import db as db_module
from notes import store, sync
from notes.okf import parse_note_text, serialize_note_text
from skills import notes as notes_skills


class _CapturingParser:
    def __init__(self):
        self.upserts = []

    async def notes_upsert(self, **kw):
        self.upserts.append(kw)
        return {"upserted": len(kw["chunks"])}

    async def notes_delete(self, user_id, note_id):
        return {"ok": True}


class _Yes:
    def register(self, *a):
        return "cid"

    async def wait(self, cid):
        return True


class _Sink:
    async def put(self, ev):
        pass


def test_full_note_lifecycle(tmp_path, monkeypatch):
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    store.set_notes_root(conn, str(tmp_path / "Notes"))
    monkeypatch.setattr(db_module, "get_connection", lambda: conn)
    cap = _CapturingParser()
    from notes import indexer
    monkeypatch.setattr(indexer, "_CLIENT", cap)

    notes_skills.USER_ID_VAR.set("1")
    notes_skills.SESSION_ID_VAR.set("s1")
    notes_skills.CONFIRM_MGR_VAR.set(_Yes())
    notes_skills.EVENT_QUEUE_VAR.set(_Sink())

    loop = asyncio.get_event_loop()
    out = json.loads(loop.run_until_complete(notes_skills._write_note_impl(
        "NAS 选型结论", "选 X 型号,理由……", "note", ["hardware"], [])))
    assert out["ok"] and out["status"] == "curated"

    # Qdrant payload 契约
    up = cap.upserts[0]
    assert up["user_id"] == "1" and up["note_type"] == "note"
    assert up["chunks"][0]["text"].startswith("# NAS 选型结论")

    # 人在外部编辑同一文件 → 扫描应升 revision 并重索引
    n = store.list_notes(conn, "1")[0]
    p = store.note_abs_path(conn, n)
    with open(p, encoding="utf-8") as f:
        meta, _ = parse_note_text(f.read())
    with open(p, "w", encoding="utf-8") as f:
        f.write(serialize_note_text(meta, "人工修订后的内容"))
    stats = loop.run_until_complete(sync.scan_once(conn))
    assert stats["updated"] == 1
    assert len(cap.upserts) == 2
    row = conn.execute("SELECT revision FROM notes WHERE id=?",
                       (n["id"],)).fetchone()
    assert row["revision"] == 2
