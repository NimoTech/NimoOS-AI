import asyncio
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from db import init_db
import notes_distill
from notes import store as notes_store


def _conn(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    notes_store.set_notes_root(conn, str(tmp_path / "Notes"))
    return conn


async def _ok(note, body):
    return True


PARSED = {"title": "Contract A", "description": "d", "body": "B", "tags": ["x"]}


def test_creates_draft_summary_with_source_ref(tmp_path):
    conn = _conn(tmp_path)
    note = asyncio.run(notes_distill.apply_distillation(
        conn, "u1", file_path="/DATA/a.pdf", root_id="r1", mtime=100,
        parsed=PARSED, truncated=False, note_indexer=_ok))
    assert note["type"] == "summary"
    assert note["status"] == "draft"
    assert note["created_by"] == "pipeline"
    assert note["source_refs"] == [{"path": "/DATA/a.pdf", "root_id": "r1",
                                    "mtime": 100, "truncated": False}]


def test_second_pass_updates_same_note(tmp_path):
    conn = _conn(tmp_path)
    first = asyncio.run(notes_distill.apply_distillation(
        conn, "u1", file_path="/DATA/a.pdf", root_id="r1", mtime=100,
        parsed=PARSED, truncated=False, note_indexer=_ok))
    second = asyncio.run(notes_distill.apply_distillation(
        conn, "u1", file_path="/DATA/a.pdf", root_id="r1", mtime=200,
        parsed={**PARSED, "body": "B2"}, truncated=False, note_indexer=_ok))
    assert second["id"] == first["id"]
    assert second["revision"] == 2
    assert second["source_refs"][0]["mtime"] == 200
    assert conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"] == 1


def test_curated_note_is_marked_stale_not_overwritten(tmp_path):
    conn = _conn(tmp_path)
    note = asyncio.run(notes_distill.apply_distillation(
        conn, "u1", file_path="/DATA/a.pdf", root_id="r1", mtime=100,
        parsed=PARSED, truncated=False, note_indexer=_ok))
    notes_store.update_note(conn, "u1", note["id"], expected_revision=1,
                            status="curated", body="human edited")
    out = asyncio.run(notes_distill.apply_distillation(
        conn, "u1", file_path="/DATA/a.pdf", root_id="r1", mtime=200,
        parsed={**PARSED, "body": "MACHINE"}, truncated=False,
        note_indexer=_ok))
    assert out["source_refs"][0]["stale"] is True
    fresh = notes_store.get_note(conn, "u1", note["id"])
    # get_note round-trips through the on-disk OKF file, which always
    # newline-terminates the body (notes/okf.py serialize_note_text) —
    # existing store tests compare with .strip() for the same reason.
    assert fresh["body"].strip() == "human edited"
    assert fresh["status"] == "curated"


def test_truncated_flag_lands_in_source_refs(tmp_path):
    conn = _conn(tmp_path)
    note = asyncio.run(notes_distill.apply_distillation(
        conn, "u1", file_path="/DATA/big.pdf", root_id="r1", mtime=1,
        parsed=PARSED, truncated=True, note_indexer=_ok))
    assert note["source_refs"][0]["truncated"] is True


def test_failed_index_leaves_pending_sentinel(tmp_path):
    conn = _conn(tmp_path)

    async def _fail(note, body):
        return False

    note = asyncio.run(notes_distill.apply_distillation(
        conn, "u1", file_path="/DATA/a.pdf", root_id="r1", mtime=1,
        parsed=PARSED, truncated=False, note_indexer=_fail))
    row = conn.execute("SELECT content_hash FROM notes WHERE id=?",
                       (note["id"],)).fetchone()
    assert row["content_hash"] == ""


def test_find_summary_note_matches_exact_path_not_substring(tmp_path):
    conn = _conn(tmp_path)
    asyncio.run(notes_distill.apply_distillation(
        conn, "u1", file_path="/DATA/a.pdf", root_id="r1", mtime=1,
        parsed=PARSED, truncated=False, note_indexer=_ok))
    assert notes_distill.find_summary_note(conn, "u1", "/DATA/a.pdf")
    assert notes_distill.find_summary_note(conn, "u1", "/DATA/a.pd") is None
