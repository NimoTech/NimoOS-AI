"""Stage 4 of the ask pipeline: turn fused candidates into a numbered evidence pack (spec §3.3.3)."""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from ask import config
from ask.prompt import EVIDENCE_INTRO
from ask.retrieve import Candidate
from fences import fence_untrusted
from skills.search.inline import fetch_small_documents, inline_eligible

_LOG = logging.getLogger(__name__)
SNIPPET_CHARS = 200


@dataclass
class EvidencePack:
    items: list[Candidate]
    dropped: int
    total_chars: int
    step_summaries: list[str] = field(default_factory=list)


def stitch(a_text: str, a_end: int | None, b_text: str, b_start: int | None) -> str:
    """Join two consecutive chunks, dropping the character overlap Parser adds
    between neighbours (offsets are character counts, as in Search's
    GetDocumentText). Unknown offsets or a gap → explicit ellipsis."""
    if a_end is not None and b_start is not None and b_start <= a_end:
        return a_text + b_text[a_end - b_start:]
    return a_text + "\n…\n" + b_text


def merge_adjacent(cands: list[Candidate]) -> list[Candidate]:
    """Merge hits of the same file whose chunk_no differ by <= 1 into one item,
    kept at the rank position of the best member."""
    by_file: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for c in cands:
        by_file[(c.file_id, c.kind)].append(c)
    consumed: set[str] = set()
    merged_for: dict[str, Candidate] = {}
    for members in by_file.values():
        if len(members) < 2 or members[0].source != "document":
            continue
        members = sorted(members, key=lambda c: c.chunk_no)
        run: list[Candidate] = [members[0]]
        runs: list[list[Candidate]] = []
        for c in members[1:]:
            if c.chunk_no - run[-1].chunk_no <= 1:
                run.append(c)
            else:
                runs.append(run)
                run = [c]
        runs.append(run)
        for r in runs:
            if len(r) < 2:
                continue
            best = max(r, key=lambda c: c.rrf)
            text = r[0].text
            for prev, cur in zip(r, r[1:]):
                text = stitch(text, prev.offset_end, cur.text, cur.offset_start)
            m = Candidate(**{**best.__dict__})
            m.chunk_no = r[0].chunk_no
            m.key = f"doc:{m.file_id}:{m.kind}:{m.chunk_no}"
            m.text = text
            m.offset_start, m.offset_end = r[0].offset_start, r[-1].offset_end
            m.merged_chunk_nos = [c.chunk_no for c in r]
            m.hit_queries = sorted({q for c in r for q in c.hit_queries})
            m.rrf = best.rrf
            for c in r:
                consumed.add(c.key)
            merged_for[best.key] = m
    out: list[Candidate] = []
    for c in cands:
        if c.key in merged_for:
            out.append(merged_for[c.key])
        elif c.key not in consumed:
            out.append(c)
    return out


def _stitch_chunks(chunks: list[dict]) -> str:
    text, prev_end = "", None
    for i, ch in enumerate(sorted(chunks, key=lambda x: int(x.get("chunk_no") or 0))):
        t = str(ch.get("text") or "")
        if i == 0:
            text = t
        else:
            text = stitch(text, prev_end, t, ch.get("offset_start"))
        prev_end = ch.get("offset_end")
    return text


async def expand_parents(cands: list[Candidate], *, invoke_tool, user_id: str,
                         max_chars: int = config.PARENT_MAX_CHARS) -> list[Candidate]:
    """>=2 hits sharing a parent_id → one item carrying the whole section
    (Search read_file_chunk parent=true), capped at max_chars. Failure keeps
    the original hits."""
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        if c.source == "document" and c.parent_id:
            groups[(c.file_id, c.parent_id)].append(c)
    replaced: dict[str, Candidate] = {}
    consumed: set[str] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        best = max(members, key=lambda c: c.rrf)
        try:
            out = await invoke_tool("read_file_chunk", {
                "file_id": best.file_id, "kind": best.kind, "chunk_no": best.chunk_no, "parent": True,
            }, user_id=user_id)
            chunks = out.get("chunks") if isinstance(out, dict) else None
            if not chunks:
                continue
            text = _stitch_chunks(chunks)
            if len(text) > max_chars:
                text = text[:max_chars] + "…"
            p = Candidate(**{**best.__dict__})
            p.text, p.parent = text, True
            p.merged_chunk_nos = sorted(int(ch.get("chunk_no") or 0) for ch in chunks)
            p.hit_queries = sorted({q for c in members for q in c.hit_queries})
            replaced[best.key] = p
            consumed.update(c.key for c in members if c.key != best.key)
        except Exception as exc:  # noqa: BLE001 — expansion is best effort
            _LOG.info("ask parent expansion failed for %s: %r", best.file_id, exc)
    out_list: list[Candidate] = []
    for c in cands:
        if c.key in replaced:
            out_list.append(replaced[c.key])
        elif c.key not in consumed:
            out_list.append(c)
    return out_list


async def inline_small_docs(cands: list[Candidate], *, invoke_tool, user_id: str) -> None:
    ids = [c.file_id for c in cands
           if c.source == "document" and not c.parent and inline_eligible(c.mime, c.kind)]
    if not ids:
        return
    texts = await fetch_small_documents(ids, invoke_tool=invoke_tool, user_id=user_id)
    for c in cands:
        t = texts.get(c.file_id)
        if t and c.source == "document":
            c.text, c.full_text = t, True


def apply_budget(cands: list[Candidate], *, max_items: int, budget_chars: int) -> tuple[list[Candidate], int]:
    kept: list[Candidate] = []
    used = 0
    for c in cands:
        if len(kept) >= max_items:
            break
        if not kept and len(c.text) > budget_chars:
            c.text = c.text[:budget_chars] + "…"      # first item always ships, truncated
        elif used + len(c.text) > budget_chars:
            continue
        kept.append(c)
        used += len(c.text)
    return kept, len(cands) - len(kept)


def _header(n: int, c: Candidate) -> str:
    pos = (f"chunks {c.merged_chunk_nos[0]}-{c.merged_chunk_nos[-1]}"
           if len(c.merged_chunk_nos) > 1 else f"chunk {c.chunk_no}")
    tags = []
    if c.full_text:
        tags.append("full text")
    if c.parent:
        tags.append(f"section: {c.section}" if c.section else "whole section")
    elif c.section:
        tags.append(f"§{c.section}")
    if c.page is not None:
        tags.append(f"page {c.page}")
    if c.seen:
        tags.append("already shown earlier in this conversation")
    if c.source == "note":
        tags.append("knowledge note")
    hit = ", ".join(f"q{q + 1}" for q in c.hit_queries)
    line = f"[{n}] {c.name}  ·  {c.path}  ·  {pos}"
    if tags:
        line += "  ·  " + " · ".join(tags)
    if hit:
        line += f"\n    (hit by: {hit})"
    return line


def render_pack(pack: EvidencePack, *, budget_chars: int) -> str:
    if not pack.items:
        return ""
    parts = [EVIDENCE_INTRO, "<evidence>"]
    if pack.step_summaries:
        parts.append("Step summaries:\n" + "\n".join(f"- {s}" for s in pack.step_summaries))
    for n, c in enumerate(pack.items, 1):
        parts.append(_header(n, c) + "\n" + c.text.strip())
    if pack.dropped:
        parts.append(f"({pack.dropped} further relevant passages were not included for space.)")
    parts.append("</evidence>")
    return fence_untrusted("evidence", "\n\n".join(parts), cap=budget_chars + 4000)


def sources_payload(pack: EvidencePack, *, total_queries: int) -> list[dict]:
    out = []
    for n, c in enumerate(pack.items, 1):
        out.append({
            "n": n, "source": c.source, "file_id": c.file_id, "name": c.name, "path": c.path,
            "kind": c.kind, "chunk_no": c.chunk_no, "merged_chunk_nos": list(c.merged_chunk_nos),
            "section": c.section, "page": c.page, "score": round(c.rrf, 6),
            "hit_queries": list(c.hit_queries), "total_queries": total_queries,
            "snippet": c.text[:SNIPPET_CHARS], "full_text": c.full_text, "parent": c.parent,
            "seen": c.seen, "note_id": c.note_id,
        })
    return out


async def build_pack(cands: list[Candidate], *, invoke_tool, user_id: str,
                     max_items: int, budget_chars: int) -> EvidencePack:
    step1 = merge_adjacent(cands)
    step2 = await expand_parents(step1, invoke_tool=invoke_tool, user_id=user_id)
    await inline_small_docs(step2, invoke_tool=invoke_tool, user_id=user_id)
    kept, dropped = apply_budget(step2, max_items=max_items, budget_chars=budget_chars)
    return EvidencePack(items=kept, dropped=dropped, total_chars=sum(len(c.text) for c in kept))
