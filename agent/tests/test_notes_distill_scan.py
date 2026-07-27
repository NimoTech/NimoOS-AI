import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from db import init_db
import notes_distill
import notes_distill_scan
from notes import store as notes_store


def _conn(tmp_path):
    conn = init_db(str(tmp_path / "m.db"))
    notes_store.set_notes_root(conn, str(tmp_path / "Notes"))
    return conn


def _mk(root, name, text="hi"):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_scan_enqueues_only_documents(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "docs"
    _mk(root, "a.pdf")
    _mk(root, "b.md")
    _mk(root, "c.py")
    _mk(root, "d.dwg")
    n = notes_distill_scan.scan_root(conn, user_id="u1", root_id="r1",
                                     root_path=str(root), known={})
    assert n == 2
    paths = {r["file_path"] for r in
             conn.execute("SELECT file_path FROM notes_distill_jobs")}
    assert paths == {str(root / "a.pdf"), str(root / "b.md")}


def test_scan_skips_unchanged_files(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "docs"
    f = _mk(root, "a.pdf")
    known = {str(f): int(f.stat().st_mtime)}
    assert notes_distill_scan.scan_root(conn, user_id="u1", root_id="r1",
                                        root_path=str(root), known=known) == 0


def test_scan_skips_hidden_and_system_dirs(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "docs"
    _mk(root, ".system_data/x.pdf")
    _mk(root, ".hidden/y.pdf")
    _mk(root, "visible.pdf")
    n = notes_distill_scan.scan_root(conn, user_id="u1", root_id="r1",
                                     root_path=str(root), known={})
    assert n == 1


def test_scan_skips_hidden_files(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "docs"
    _mk(root, ".wiki.md")   # Wiki's per-directory nav map — distillable ext, hidden name
    _mk(root, "a.md")
    n = notes_distill_scan.scan_root(conn, user_id="u1", root_id="r1",
                                     root_path=str(root), known={})
    assert n == 1
    row = conn.execute("SELECT file_path FROM notes_distill_jobs").fetchone()
    assert row["file_path"] == str(root / "a.md")


def test_scan_missing_root_is_not_fatal(tmp_path):
    conn = _conn(tmp_path)
    assert notes_distill_scan.scan_root(conn, user_id="u1", root_id="r1",
                                        root_path=str(tmp_path / "nope"),
                                        known={}) == 0


def test_scan_once_only_visits_opted_in_roots(tmp_path):
    conn = _conn(tmp_path)
    on, off = tmp_path / "on", tmp_path / "off"
    _mk(on, "a.pdf")
    _mk(off, "b.pdf")
    notes_store.set_distill_roots(conn, "u1", ["r-on"])
    n = notes_distill_scan.scan_once(conn, user_id="u1", roots=[
        {"id": "r-on", "path": str(on), "enabled": True},
        {"id": "r-off", "path": str(off), "enabled": True},
    ])
    assert n == 1
    row = conn.execute("SELECT file_path FROM notes_distill_jobs").fetchone()
    assert row["file_path"] == str(on / "a.pdf")


def test_scan_once_skips_disabled_root(tmp_path):
    conn = _conn(tmp_path)
    on = tmp_path / "on"
    _mk(on, "a.pdf")
    notes_store.set_distill_roots(conn, "u1", ["r-on"])
    assert notes_distill_scan.scan_once(conn, user_id="u1", roots=[
        {"id": "r-on", "path": str(on), "enabled": False}]) == 0


def test_scan_reenqueues_when_file_newer_than_known(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "docs"
    f = _mk(root, "a.pdf")
    known = {str(f): int(f.stat().st_mtime) - 10}   # we distilled an older version
    n = notes_distill_scan.scan_root(conn, user_id="u1", root_id="r1",
                                     root_path=str(root), known=known)
    assert n == 1
    row = conn.execute("SELECT file_mtime FROM notes_distill_jobs").fetchone()
    assert row["file_mtime"] == int(f.stat().st_mtime)


def test_opted_in_users_excludes_users_who_cleared_their_roots(tmp_path):
    conn = _conn(tmp_path)
    notes_store.set_distill_roots(conn, "u1", ["r-on"])
    notes_store.set_background_model(conn, "u1", "cloud:1:m")
    notes_store.set_distill_roots(conn, "u2", [])   # touched, then cleared
    assert notes_distill_scan._opted_in_users(conn) == ["u1"]


def test_opted_in_users_excludes_users_with_empty_background_model(tmp_path):
    """Roots opted in but no background model configured = feature silent
    (notes_store.get_background_model docstring) — the scanner must not walk
    the filesystem and enqueue jobs process_pending_once would immediately
    tombstone as 'skipped'."""
    conn = _conn(tmp_path)
    notes_store.set_distill_roots(conn, "u1", ["r-on"])
    # deliberately never call set_background_model for u1
    assert notes_distill_scan._opted_in_users(conn) == []


def test_scan_root_does_not_reenqueue_a_failed_tombstone_for_unchanged_file(
        tmp_path):
    """C3: a job tombstoned as 'failed' (attempts exhausted) must stay out of
    the re-enqueue path as long as the file itself hasn't changed — otherwise
    a poison document loops forever every scan pass."""
    conn = _conn(tmp_path)
    root = tmp_path / "docs"
    f = _mk(root, "a.pdf")
    mtime = int(f.stat().st_mtime)
    notes_distill.enqueue(conn, file_path=str(f), user_id="u1", root_id="r1",
                          file_mtime=mtime, now=1)
    notes_distill.claim_job(conn, quota_ok=True, now=2)
    notes_distill.fail_job(conn, str(f), notes_distill.MAX_ATTEMPTS,
                           ValueError("boom"), 3)
    row = conn.execute("SELECT status FROM notes_distill_jobs").fetchone()
    assert row["status"] == "failed"

    known = notes_distill_scan._known_mtimes(conn, "u1")
    n = notes_distill_scan.scan_root(conn, user_id="u1", root_id="r1",
                                     root_path=str(root), known=known)
    assert n == 0
    assert conn.execute("SELECT status FROM notes_distill_jobs"
                        ).fetchone()["status"] == "failed"


def test_enqueue_flips_a_tombstone_back_to_pending_when_file_is_newer(
        tmp_path):
    """The enqueue UPSERT (ON CONFLICT(file_path)) is the ONLY intended path
    back to 'pending' for a tombstoned row: the file changed, or a manual
    re-POST. Exercise it via the scanner path (scan_root -> enqueue) with a
    'failed' row whose known mtime is now stale."""
    conn = _conn(tmp_path)
    root = tmp_path / "docs"
    f = _mk(root, "a.pdf")
    old_mtime = int(f.stat().st_mtime)
    notes_distill.enqueue(conn, file_path=str(f), user_id="u1", root_id="r1",
                          file_mtime=old_mtime, now=1)
    notes_distill.claim_job(conn, quota_ok=True, now=2)
    notes_distill.fail_job(conn, str(f), notes_distill.MAX_ATTEMPTS,
                           ValueError("boom"), 3)
    assert conn.execute("SELECT status FROM notes_distill_jobs"
                        ).fetchone()["status"] == "failed"

    known = {str(f): old_mtime - 10}   # simulate the file having been edited
    n = notes_distill_scan.scan_root(conn, user_id="u1", root_id="r1",
                                     root_path=str(root), known=known)
    assert n == 1
    row = conn.execute("SELECT status, attempts FROM notes_distill_jobs"
                       ).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 0


def test_known_mtimes_from_existing_summary_note_prevents_reenqueue(
        tmp_path):
    conn = _conn(tmp_path)
    from notes import store as ns
    note = ns.create_note(
        conn, "u1", title="T", body="B", note_type="summary", tags=[],
        source_refs=[{"path": "/DATA/a.pdf", "root_id": "r1", "mtime": 500,
                      "truncated": False}],
        created_by="pipeline", description="")
    assert note["id"]
    known = notes_distill_scan._known_mtimes(conn, "u1")
    assert known["/DATA/a.pdf"] == 500


def test_known_mtimes_includes_tombstoned_job_rows(tmp_path):
    conn = _conn(tmp_path)
    notes_distill.enqueue(conn, file_path="/DATA/a.pdf", user_id="u1",
                          root_id="r1", file_mtime=42, now=1)
    notes_distill.claim_job(conn, quota_ok=True, now=2)
    notes_distill.fail_job(conn, "/DATA/a.pdf", notes_distill.MAX_ATTEMPTS,
                           ValueError("boom"), 3)
    assert conn.execute("SELECT status FROM notes_distill_jobs"
                        ).fetchone()["status"] == "failed"
    known = notes_distill_scan._known_mtimes(conn, "u1")
    assert known["/DATA/a.pdf"] == 42
