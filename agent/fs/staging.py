"""staged_changes table mutation + commit/revert orchestration."""
from __future__ import annotations

import errno
import os
import sqlite3
import time
from typing import Optional

from audit import audit as _audit


def record(conn: sqlite3.Connection, session_id: str, run_id: str,
           seq: int, op: str, path: str, *,
           dst_path: Optional[str] = None,
           snapshot_path: Optional[str] = None,
           snapshot_kind: Optional[str] = None,
           original_uid: Optional[int] = None,
           original_gid: Optional[int] = None,
           original_mode: Optional[int] = None,
           size_bytes: Optional[int] = None,
           batch_id: Optional[str] = None) -> int:
    cur = conn.execute(
        "INSERT INTO staged_changes "
        "(session_id, run_id, seq, op, path, dst_path, snapshot_path, "
        " snapshot_kind, original_uid, original_gid, original_mode, "
        " size_bytes, status, created_at, batch_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, run_id, seq, op, path, dst_path, snapshot_path,
         snapshot_kind, original_uid, original_gid, original_mode,
         size_bytes, "pending", int(time.time()), batch_id),
    )
    conn.commit()
    return cur.lastrowid


def maybe_take_file_snapshot(conn, store, session_id: str, run_id: str,
                              seq: str, abs_path: str) -> Optional[str]:
    """Take a file snapshot only if no earlier op in this run already snapshotted
    this exact path. Returns the snapshot path (existing or new)."""
    row = conn.execute(
        "SELECT snapshot_path FROM staged_changes "
        "WHERE run_id=? AND path=? AND snapshot_path IS NOT NULL "
        "ORDER BY seq ASC LIMIT 1",
        (run_id, abs_path),
    ).fetchone()
    if row is not None:
        return row["snapshot_path"]
    return store.take_file(session_id, run_id, seq, abs_path)


def commit_session(conn: sqlite3.Connection, store, session_id: str) -> None:
    cur = conn.execute(
        "UPDATE staged_changes SET status='committed' "
        "WHERE session_id=? AND status='pending'",
        (session_id,),
    )
    conn.commit()
    # L4: committing staged file changes is a real, durable disk action — audit it.
    try:
        _audit("fs_commit", session_id=session_id, committed=cur.rowcount)
    except Exception:  # noqa: BLE001 — audit must never break the commit
        pass
    store.prune_session(session_id)


def revert_run(conn: sqlite3.Connection, store, session_id: str,
               run_id: str) -> dict:
    rows = conn.execute(
        "SELECT id, seq, op, path, dst_path, snapshot_path, snapshot_kind, "
        "       original_uid, original_gid, original_mode "
        "FROM staged_changes "
        "WHERE session_id=? AND run_id=? AND status='pending' "
        "ORDER BY seq DESC",
        (session_id, run_id),
    ).fetchall()

    if not rows:
        return {"status": "nothing_to_revert"}

    # Pre-check: any row whose op needs a snapshot but file is gone
    for r in rows:
        op = r["op"]
        sp = r["snapshot_path"]
        needs_snap = op in ("write", "edit", "delete_file") or (
            op == "delete_dir" and sp is not None
        )
        if needs_snap and (not sp or not os.path.exists(sp)):
            return {"status": "snapshot_missing", "row_id": r["id"]}

    failed: list[dict] = []
    for r in rows:
        try:
            _replay_reverse(store, r)
            conn.execute(
                "UPDATE staged_changes SET status='reverted' WHERE id=?",
                (r["id"],),
            )
            conn.commit()
            try:  # L4: reverting a staged change mutates disk — audit it
                _audit("fs_revert", session_id=session_id, op=r["op"],
                       path=r["path"], dst_path=r["dst_path"])
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:
            failed.append({"id": r["id"], "op": r["op"], "path": r["path"],
                           "error": str(e)})

    store.prune_run(session_id, run_id)

    if failed:
        return {"status": "partial", "failed": failed}
    return {"status": "ok"}


def _revert_rows(conn, store, rows) -> dict:
    """Replay reverse over the given rows (must already be ordered by seq DESC).
    Reuses the same snapshot pre-check and _replay_reverse as revert_run."""
    # snapshot pre-check (mirror revert_run's check)
    for r in rows:
        op, sp = r["op"], r["snapshot_path"]
        needs_snap = op in ("write", "edit", "delete_file") or (
            op == "delete_dir" and sp is not None)
        if needs_snap and (not sp or not os.path.exists(sp)):
            return {"status": "snapshot_missing", "row_id": r["id"]}
    failed = []
    for r in rows:
        try:
            _replay_reverse(store, r)
            conn.execute("UPDATE staged_changes SET status='reverted' WHERE id=?",
                         (r["id"],))
            conn.commit()
            try:  # L4: audit each reverted change (rows come from SELECT *)
                _audit("fs_revert", session_id=r["session_id"], op=r["op"],
                       path=r["path"], dst_path=r["dst_path"])
            except Exception:  # noqa: BLE001
                pass
        except OSError as e:
            if getattr(e, "errno", None) == errno.ENOTEMPTY:
                return {"status": "conflict",
                        "reason": "目录非空,请先撤销移入的文件", "row_id": r["id"]}
            failed.append({"id": r["id"], "op": r["op"], "error": str(e)})
    if failed:
        return {"status": "partial", "failed": failed}
    return {"status": "ok"}


def revert_batch(conn, store, session_id: str, batch_id: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM staged_changes WHERE session_id=? AND batch_id=? "
        "AND status='pending' ORDER BY seq DESC", (session_id, batch_id)).fetchall()
    if not rows:
        return {"status": "nothing_to_revert"}
    return _revert_rows(conn, store, rows)


def revert_items(conn, store, session_id: str, staged_ids: list) -> dict:
    if not staged_ids:
        return {"status": "nothing_to_revert"}
    qmarks = ",".join("?" * len(staged_ids))
    rows = conn.execute(
        f"SELECT * FROM staged_changes WHERE session_id=? AND id IN ({qmarks}) "
        f"AND status='pending' ORDER BY seq DESC", (session_id, *staged_ids)
    ).fetchall()
    if not rows:
        return {"status": "nothing_to_revert"}
    return _revert_rows(conn, store, rows)


def _replay_reverse(store, row) -> None:
    op = row["op"]
    path = row["path"]
    if op in ("write", "edit"):
        store.restore_file(row["snapshot_path"], path)
        _restore_perms(path, row)
    elif op == "delete_file":
        store.restore_file(row["snapshot_path"], path)
        _restore_perms(path, row)
    elif op == "delete_dir":
        if row["snapshot_kind"] == "tar" and row["snapshot_path"]:
            store.restore_tar(row["snapshot_path"], path)
        else:
            os.makedirs(path, exist_ok=True)
            if row["original_mode"] is not None:
                os.chmod(path, row["original_mode"])
    elif op == "mkdir":
        os.rmdir(path)  # later seqs were processed first; should now be empty
    elif op == "rename":
        os.rename(row["dst_path"], path)
    else:
        raise ValueError(f"unknown op: {op}")


def _restore_perms(path: str, row) -> None:
    uid = row["original_uid"]
    gid = row["original_gid"]
    mode = row["original_mode"]
    try:
        if uid is not None and gid is not None:
            os.chown(path, uid, gid)
    except PermissionError:
        pass
    if mode is not None:
        os.chmod(path, mode)
