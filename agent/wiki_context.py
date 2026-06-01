"""Build the Wiki context block prepended to every chat turn's system prompt.

Two markdown blocks:
  ## NimoOS 存储空间地图  — space/project skeleton, top-15 projects per root
  ## 用户笔记           — user_notes for nodes updated in the last 30 days,
                          per-node truncated to 500 chars, total 2000 chars

Wiki unreachable → return placeholder; the tools still answer "wiki service
unavailable" on demand.
"""
from __future__ import annotations

import os
import time

import pathspec

from wiki_client import WikiClient


PROJECT_CAP = 15
USER_NOTES_ACTIVE_DAYS = 30
USER_NOTES_PER_NODE_CHAR_CAP = 500
USER_NOTES_TOTAL_CHAR_CAP = 2000


class WikiContextBuilder:
    def __init__(self, client: WikiClient) -> None:
        self.client = client

    async def build(self, user_patterns: list[str]) -> str:
        try:
            tree = await self.client.list_full_tree()
        except Exception:
            return "## NimoOS 存储空间地图\n_(Wiki 服务暂不可用)_\n"

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
        lines = ["## NimoOS 存储空间地图", ""]

        if not spaces:
            lines.append(
                "_(暂无注册的 Wiki 空间。Wiki 不会自动扫描文件树 —— "
                "若用户希望某个目录被纳入,主动建议调用 `wiki_register_root` "
                "登记。)_"
            )
            return "\n".join(lines)

        lines.append(
            "_(下面是用户已显式登记到 Wiki 的空间/项目。Wiki 不会自动扫描文件树 —— "
            "若用户提到的路径不在下面,你应主动询问是否调用 `wiki_register_root` "
            "把它登记进来。)_"
        )
        lines.append("")

        for space in spaces:
            sp = space["path"]
            label = space.get("ai_label") or os.path.basename(sp) or sp
            lines.append(f"- **{sp}** (space) — {label}")
            projects = [
                n for n in tree
                if n.get("level") == "project" and n["path"].startswith(sp + "/")
            ]
            projects.sort(key=lambda n: n.get("last_modified_ms", 0), reverse=True)
            for n in projects[:PROJECT_CAP]:
                lbl = n.get("ai_label")
                if not lbl:
                    lbl = os.path.basename(n["path"]) + " (未生成摘要)"
                lines.append(f"  - {n['path']} — {lbl}")
            if len(projects) > PROJECT_CAP:
                extra = len(projects) - PROJECT_CAP
                lines.append(
                    f"  - ... 还有 {extra} 个项目,"
                    f"用 wiki_list_full_tree(root_id='{sp}') 查看完整列表"
                )
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

        lines = ["## 用户笔记", ""]
        if not active:
            lines.append("_(最近 30 天无活跃笔记)_")
            return "\n".join(lines)

        total = 0
        appended = 0
        for n in active:
            if total >= USER_NOTES_TOTAL_CHAR_CAP:
                remaining = len(active) - appended
                lines.append(
                    f"\n_(余 {remaining} 条笔记略;用 wiki_get_node('{n['path']}') 等查看)_"
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
                        + f"\n…(更多见 wiki_get_node('{n['path']}'))")
            lines.append(f"### {n['path']}")
            lines.append(body)
            lines.append("")
            total += len(body)
            appended += 1
        return "\n".join(lines)
