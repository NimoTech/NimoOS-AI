"""Sidecar snapshot store under /var/lib/nimoos/ai/agent/snapshots/.

Layout:
  <root>/<session_id>/<run_id>/<seq>-<sha1(path)>.bin   for files
  <root>/<session_id>/<run_id>/<seq>-<sha1(path)>.tar.gz for directories
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile


DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB


class SnapshotTooLarge(Exception):
    pass


class SnapshotStore:
    def __init__(self, root: str = "/var/lib/nimoos/ai/agent/snapshots",
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self._root = root
        self._max_bytes = max_bytes
        os.makedirs(self._root, exist_ok=True)

    def _run_dir(self, session_id: str, run_id: str) -> str:
        d = os.path.join(self._root, session_id, run_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _name(self, seq: str, abs_path: str, ext: str) -> str:
        sha = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:12]
        return f"{seq}-{sha}{ext}"

    def take_file(self, session_id: str, run_id: str, seq: str,
                  abs_path: str) -> str:
        size = os.path.getsize(abs_path)
        if size > self._max_bytes:
            raise SnapshotTooLarge(
                f"file {abs_path} is {size} bytes; cap is {self._max_bytes}")
        out = os.path.join(self._run_dir(session_id, run_id),
                           self._name(seq, abs_path, ".bin"))
        # Copy preserving permissions; we don't need them on revert (we save
        # uid/gid/mode in DB row), but copy2 is the well-known atomic-ish API.
        shutil.copy2(abs_path, out)
        return out

    def take_tar(self, session_id: str, run_id: str, seq: str,
                 abs_dir: str) -> str:
        # First pass: cumulative size check
        total = 0
        for dp, _, files in os.walk(abs_dir):
            for f in files:
                fp = os.path.join(dp, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
                if total > self._max_bytes:
                    raise SnapshotTooLarge(
                        f"directory {abs_dir} exceeds cap of {self._max_bytes}")
        out = os.path.join(self._run_dir(session_id, run_id),
                           self._name(seq, abs_dir, ".tar.gz"))
        with tarfile.open(out, "w:gz") as tf:
            tf.add(abs_dir, arcname=os.path.basename(abs_dir))
        return out

    def restore_file(self, snapshot_path: str, dest_path: str) -> None:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(snapshot_path, dest_path)

    def restore_tar(self, snapshot_path: str, dest_dir: str) -> None:
        # Extract to parent of dest_dir; tar archive contains a top-level entry
        # named basename(dest_dir).
        parent = os.path.dirname(dest_dir.rstrip(os.sep))
        os.makedirs(parent, exist_ok=True)
        with tarfile.open(snapshot_path, "r:gz") as tf:
            # data_filter (Python 3.12+) defends against path-traversal in
            # archive entries; fall back gracefully on older runtimes.
            try:
                tf.extractall(parent, filter="data")
            except TypeError:
                tf.extractall(parent)

    def prune_run(self, session_id: str, run_id: str) -> None:
        shutil.rmtree(os.path.join(self._root, session_id, run_id),
                      ignore_errors=True)

    def prune_session(self, session_id: str) -> None:
        shutil.rmtree(os.path.join(self._root, session_id),
                      ignore_errors=True)
