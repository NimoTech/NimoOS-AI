"""Build the Wiki context block prepended to every chat turn's system prompt.

Two markdown blocks:
  ## NimoOS storage map — space/project skeleton, top-15 projects per root
  ## User notes         — user_notes for nodes updated in the last 30 days,
                          per-node truncated to 500 chars, total 2000 chars

Wiki unreachable → return placeholder; the tools still answer "wiki service
unavailable" on demand.
"""
from __future__ import annotations

import os
import re as _re
import time

import pathspec

from fences import fence_untrusted
from wiki_client import WikiClient


PROJECT_CAP = 15
USER_NOTES_ACTIVE_DAYS = 30
USER_NOTES_PER_NODE_CHAR_CAP = 500
USER_NOTES_TOTAL_CHAR_CAP = 2000

# Registered wiki node names (paths / labels) are attacker-influenceable — a
# maliciously named directory could carry `<`, `>`, or newlines and smuggle a
# stray `</untrusted-data>` close tag or an extra instruction line into the
# scaffold (which is emitted OUTSIDE the per-note fence). Strip control chars +
# angle brackets and collapse newlines before interpolating these structural
# bits. Mirrors the fences charset; used for headers/labels, not fenced bodies.
_SCAF_RE = _re.compile(r"[\x00-\x1f\x7f<>]")


def _scaf(s) -> str:
    return _SCAF_RE.sub("", str(s)).replace("\n", " ")


class WikiContextBuilder:
    def __init__(self, client: WikiClient) -> None:
        self.client = client

    async def build(self, user_patterns: list[str]) -> str:
        try:
            tree = await self.client.list_full_tree()
        except Exception:
            return "## NimoOS storage map\n_(Wiki service temporarily unavailable)_\n"

        tree = self._filter(tree, user_patterns)
        map_block = self._render_map(tree)
        notes_block = await self._render_notes(tree)
        return f"{map_block}\n\n{notes_block}"

    @staticmethod
    def _filter(tree: list[dict], patterns: list[str]) -> list[dict]:
        if not patterns:
            return tree
        spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        return [n for n in tree if not spec.match_file(n["path"].lstrip("/"))]

    def _render_map(self, tree: list[dict]) -> str:
        # Group nodes by their space ancestor. A "space" is a top-level root;
        # children are projects under it.
        spaces = [n for n in tree if n.get("level") == "space"]
        spaces.sort(key=lambda n: n["path"])
        lines = ["## NimoOS storage map", ""]

        if not spaces:
            lines.append(
                "_(No registered wiki spaces yet. The wiki does not scan the file tree "
                "automatically — if the user wants a directory included, proactively "
                "suggest calling `wiki_register_root` to register it.)_"
            )
            return "\n".join(lines)

        lines.append(
            "_(Below are the spaces/projects the user has explicitly registered into "
            "the wiki. The wiki does not scan the file tree automatically — if a path "
            "the user mentions is not listed below, proactively ask whether to "
            "register it via `wiki_register_root`.)_"
        )
        lines.append("")

        # Node lines carry ai_label / paths that are attacker-influenceable
        # (auto-generated from names a downloaded file can control). _scaf()
        # stops tag-breakout but NOT plain natural-language injection sitting in
        # what reads as trusted scaffolding — so the whole node listing is fenced
        # as data. The header/instructions above stay outside the fence (trusted).
        node_lines: list[str] = []
        for space in spaces:
            sp = space["path"]
            label = space.get("ai_label") or os.path.basename(sp) or sp
            node_lines.append(f"- **{_scaf(sp)}** (space) — {_scaf(label)}")
            projects = [
                n for n in tree
                if n.get("level") == "project" and n["path"].startswith(sp + "/")
            ]
            projects.sort(key=lambda n: n.get("last_modified_ms", 0), reverse=True)
            for n in projects[:PROJECT_CAP]:
                lbl = n.get("ai_label")
                if not lbl:
                    lbl = os.path.basename(n["path"]) + " (no summary yet)"
                node_lines.append(f"  - {_scaf(n['path'])} — {_scaf(lbl)}")
            if len(projects) > PROJECT_CAP:
                extra = len(projects) - PROJECT_CAP
                node_lines.append(
                    f"  - ... plus {extra} more items; "
                    f"use wiki_list_full_tree(root_id='{_scaf(sp)}') for the full list"
                )
        body = "\n".join(node_lines)
        lines.append(fence_untrusted("wiki-map", body, cap=8000) or body)
        return "\n".join(lines)

    async def _render_notes(self, tree: list[dict]) -> str:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - USER_NOTES_ACTIVE_DAYS * 86_400_000
        active = [
            n for n in tree
            if int(n.get("user_notes_updated_at") or 0) > cutoff
        ]
        active.sort(
            key=lambda n: int(n.get("user_notes_updated_at") or 0),
            reverse=True,
        )

        lines = ["## User notes", ""]
        if not active:
            lines.append("_(no active notes in the last 30 days)_")
            return "\n".join(lines)

        total = 0
        appended = 0
        for n in active:
            if total >= USER_NOTES_TOTAL_CHAR_CAP:
                remaining = len(active) - appended
                lines.append(
                    f"\n_({remaining} more notes omitted; use wiki_get_node('{_scaf(n['path'])}') etc. to view)_"
                )
                break
            try:
                node = await self.client.get_node(n["path"])
            except Exception:
                continue
            if node is None:
                continue
            body = (node.get("user_notes") or "").strip()
            if not body:
                continue
            if len(body) > USER_NOTES_PER_NODE_CHAR_CAP:
                body = (body[:USER_NOTES_PER_NODE_CHAR_CAP]
                        + f"\n…(more via wiki_get_node('{_scaf(n['path'])}'))")
            lines.append(f"### {_scaf(n['path'])}")
            lines.append(fence_untrusted(f"wiki:{n['path']}", body))
            lines.append("")
            total += len(body)
            appended += 1
        return "\n".join(lines)
