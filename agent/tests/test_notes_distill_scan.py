import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from db import init_db
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
    notes_store.set_distill_roots(conn, "u2", [])   # touched, then cleared
    assert notes_distill_scan._opted_in_users(conn) == ["u1"]
