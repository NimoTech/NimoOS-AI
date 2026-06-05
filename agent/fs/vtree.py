"""In-memory virtual filesystem tree for batch preflight.

Models the *post-state* of a sequence of structural ops (mkdir/rename/delete)
on top of the real disk, so each op can be validated against the cumulative
effect of earlier ops in the same batch — WITHOUT touching disk.

Hydration is lazy and read-only. Each node remembers its `origin`: the real
disk path it maps to (None for nodes created in-batch via mkdir). Hydration
scans `origin`, so a subtree moved by rename keeps hydrating from its true
on-disk location at any depth.
"""
from __future__ import annotations

import os
from typing import Dict, Optional


class VTreeError(Exception):
    """A batch op is invalid against the current virtual tree state."""


class _Node:
    __slots__ = ("is_dir", "children", "loaded", "origin")

    def __init__(self, is_dir: bool, origin: Optional[str] = None):
        self.is_dir = is_dir
        self.children: Dict[str, "_Node"] = {}
        self.loaded = False          # whether real children were hydrated
        self.origin = origin         # real disk path; None if virtual-only


class VTree:
    def __init__(self) -> None:
        self._root = _Node(is_dir=True, origin=os.sep)

    # ---------- internal walk / hydrate ----------

    def _split(self, path: str) -> list[str]:
        norm = os.path.normpath(os.path.abspath(path))
        if norm == os.sep:
            return []
        return [p for p in norm.strip(os.sep).split(os.sep) if p]

    def _hydrate_children(self, node: _Node) -> None:
        """Populate node.children from the real disk once, scanning node.origin."""
        if node.loaded or not node.is_dir:
            node.loaded = True
            return
        if node.origin is not None:
            try:
                with os.scandir(node.origin) as it:
                    for entry in it:
                        if entry.name not in node.children:
                            node.children[entry.name] = _Node(
                                is_dir=entry.is_dir(follow_symlinks=False),
                                origin=os.path.join(node.origin, entry.name))
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                pass
        node.loaded = True

    def _find(self, path: str) -> Optional[_Node]:
        """Return the node for path, hydrating ancestors along the way, or None
        if path does not exist in the virtual tree."""
        node = self._root
        for name in self._split(path):
            self._hydrate_children(node)
            child = node.children.get(name)
            if child is None:
                return None
            node = child
        return node

    def _parent_dir_node(self, path: str) -> Optional[_Node]:
        parent = os.path.dirname(os.path.normpath(os.path.abspath(path)))
        node = self._find(parent)
        if node is None or not node.is_dir:
            return None
        return node

    # ---------- queries ----------

    def exists(self, path: str) -> bool:
        return self._find(path) is not None

    def is_dir(self, path: str) -> bool:
        n = self._find(path)
        return n is not None and n.is_dir

    def is_empty_dir(self, path: str) -> bool:
        n = self._find(path)
        if n is None or not n.is_dir:
            return False
        self._hydrate_children(n)
        return len(n.children) == 0

    # ---------- mutations (validate + apply) ----------

    def mkdir(self, path: str, parents: bool = False) -> None:
        abs_path = os.path.normpath(os.path.abspath(path))
        if self.exists(abs_path):
            raise VTreeError(f"already exists: {abs_path}")
        parts = self._split(abs_path)
        if not parts:
            raise VTreeError("cannot mkdir filesystem root")
        node = self._root
        for i, name in enumerate(parts):
            self._hydrate_children(node)
            child = node.children.get(name)
            last = i == len(parts) - 1
            if child is None:
                if not last and not parents:
                    raise VTreeError(f"parent does not exist: {name}")
                child = _Node(is_dir=True, origin=None)
                child.loaded = True  # freshly created dir is empty + fully known
                node.children[name] = child
            elif not child.is_dir:
                raise VTreeError(f"path component is not a dir: {name}")
            node = child

    def rename(self, src: str, dst: str) -> None:
        src_abs = os.path.normpath(os.path.abspath(src))
        dst_abs = os.path.normpath(os.path.abspath(dst))
        # circular / self-containment (Linux rename(2) EINVAL)
        if dst_abs == src_abs or dst_abs.startswith(src_abs + os.sep):
            raise VTreeError(
                f"cannot move a directory into itself: {src_abs} -> {dst_abs}")
        src_node = self._find(src_abs)
        if src_node is None:
            raise VTreeError(f"src does not exist: {src_abs}")
        if self.exists(dst_abs):
            raise VTreeError(f"dst already exists: {dst_abs}")
        dst_parent = self._parent_dir_node(dst_abs)
        if dst_parent is None:
            raise VTreeError(f"dst parent dir does not exist: "
                             f"{os.path.dirname(dst_abs)}")
        # detach src from its parent; the node keeps its origin so its real
        # children stay hydratable from the original disk location at any depth.
        src_parent = self._parent_dir_node(src_abs)
        if src_parent is None:
            raise VTreeError(f"cannot move: {src_abs}")
        src_parent.children.pop(os.path.basename(src_abs), None)
        dst_parent.children[os.path.basename(dst_abs)] = src_node

    def delete(self, path: str, recursive: bool = False) -> None:
        abs_path = os.path.normpath(os.path.abspath(path))
        if not self._split(abs_path):
            raise VTreeError("cannot delete filesystem root")
        node = self._find(abs_path)
        if node is None:
            raise VTreeError(f"does not exist: {abs_path}")
        if node.is_dir and not recursive and not self.is_empty_dir(abs_path):
            raise VTreeError(f"directory not empty: {abs_path}")
        parent = self._parent_dir_node(abs_path)
        if parent is None:
            raise VTreeError(f"cannot delete: {abs_path}")
        parent.children.pop(os.path.basename(abs_path), None)
