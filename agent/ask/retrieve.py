"""Stage 2-3 of the ask pipeline: fan-out retrieval, dedupe, RRF fusion, MECE (spec §3.3.2)."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from ask import config

_LOG = logging.getLogger(__name__)


@dataclass
class Candidate:
    key: str
    source: str
    file_id: str
    kind: str
    chunk_no: int
    text: str
    name: str
    path: str
    mime: str = ""
    parent_id: str = ""
    section: str = ""
    page: int | None = None
    offset_start: int | None = None
    offset_end: int | None = None
    hit_queries: list[int] = field(default_factory=list)
    raw_scores: list[float] = field(default_factory=list)
    rrf: float = 0.0
    seen: bool = False
    note_id: str = ""
    merged_chunk_nos: list[int] = field(default_factory=list)
    full_text: bool = False
    parent: bool = False


def _int_or_none(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def candidate_from_hit(hit: dict) -> Candidate | None:
    if not isinstance(hit, dict):
        return None
    fid = str(hit.get("file_id") or "")
    text = str(((hit.get("preview") or {}).get("text")) or "")
    if not fid or not text.strip():
        return None
    kind = str(hit.get("kind") or "body")
    cite = hit.get("cite") or {}
    chunk_no = _int_or_none(cite.get("chunk_no")) or 0
    paths = hit.get("paths") or []
    path = str((paths[0] or {}).get("path") or "") if paths else ""
    name = os.path.basename(path) or fid
    return Candidate(
        key=f"doc:{fid}:{kind}:{chunk_no}", source="document", file_id=fid, kind=kind,
        chunk_no=chunk_no, text=text, name=name, path=path, mime=str(hit.get("mime") or ""),
        parent_id=str(hit.get("parent_id") or ""), section=str(hit.get("section") or ""),
        page=_int_or_none(cite.get("page")), offset_start=_int_or_none(cite.get("offset_start")),
        offset_end=_int_or_none(cite.get("offset_end")),
        raw_scores=[float(hit.get("score") or 0.0)], merged_chunk_nos=[chunk_no])


def candidate_from_note(hit: dict, *, title: str = "") -> Candidate | None:
    if not isinstance(hit, dict):
        return None
    nid = str(hit.get("note_id") or "")
    text = str(hit.get("text") or "")
    if not nid or not text.strip():
        return None
    chunk_no = _int_or_none(hit.get("chunk_no")) or 0
    return Candidate(
        key=f"note:{nid}:{chunk_no}", source="note", file_id=f"note:{nid}", kind="note",
        chunk_no=chunk_no, text=text, name=title or f"Note {nid[:8]}", path=f"notes://{nid}",
        mime="text/markdown", note_id=nid, raw_scores=[float(hit.get("score") or 0.0)],
        merged_chunk_nos=[chunk_no])


def rrf_fuse(ranked_lists: list[list[Candidate]], *, k: int = config.RRF_K,
             weights: list[float] | None = None) -> list[Candidate]:
    """Reciprocal rank fusion across per-query ranked lists; dedupes by key,
    records which lists hit each candidate. Stable on ties (first list wins)."""
    merged: dict[str, Candidate] = {}
    for qi, ranked in enumerate(ranked_lists):
        w = weights[qi] if weights and qi < len(weights) else 1.0
        for rank, c in enumerate(ranked):
            cur = merged.get(c.key)
            if cur is None:
                cur = c
                cur.hit_queries = []
                cur.rrf = 0.0
                merged[c.key] = cur
            else:
                cur.raw_scores.extend(c.raw_scores)
                if len(c.text) > len(cur.text):
                    cur.text = c.text
            if qi not in cur.hit_queries:
                cur.hit_queries.append(qi)
            cur.rrf += w / (k + rank + 1)
    return sorted(merged.values(), key=lambda c: -c.rrf)


def apply_mece(cands: list[Candidate], seen_keys: set[str], *,
               min_keep: int = config.MECE_MIN_KEEP) -> list[Candidate]:
    """Drop chunks already fed to the model earlier in this session; if that
    leaves fewer than min_keep, backfill with the best seen ones (flagged)."""
    fresh = [c for c in cands if c.key not in seen_keys]
    if len(fresh) >= min_keep:
        return fresh
    backfill = [c for c in cands if c.key in seen_keys]
    for c in backfill:
        c.seen = True
    return fresh + backfill[: max(0, min_keep - len(fresh))]


@dataclass
class RetrieveResult:
    candidates: list[Candidate]
    per_query_hits: list[int]
    warnings: list[str]
    all_failed: bool
    partial: bool


async def retrieve(plan, *, question: str, user_id: str, search, parser, deadline: float,
                   include_draft_notes: bool = False, note_title=None) -> RetrieveResult:
    queries = [q.q for q in plan.queries]
    statuses = ["curated", "draft"] if include_draft_notes else ["curated"]

    async def one_query(qi: int, q: str):
        # Rerank only the primary sub-query. Search's cross-encoder costs
        # ~1.3s per candidate on a CPU NAS, and every parallel sub-query
        # shares one 15s wall clock (G1: answer start <= 15s p50), so
        # reranking all of them made `partial` the normal outcome. The fused
        # list still gets cross-encoder order where it matters most and
        # vector order elsewhere.
        out = await search.search_text(q, user_id=user_id, top_k=config.SEARCH_TOP_K,
                                       rerank=(qi == 0),
                                       timeout_s=config.PER_QUERY_TIMEOUT_S)
        cands = [c for c in (candidate_from_hit(h) for h in out.get("hits") or []) if c]
        return qi, cands, list(out.get("warnings") or [])

    async def notes():
        out = await parser.notes_query(user_id, question, top_k=config.NOTES_TOP_K, statuses=statuses)
        cands = []
        for h in out.get("hits") or []:
            title = ""
            if note_title is not None:
                try:
                    title = note_title(str(h.get("note_id") or "")) or ""
                except Exception:  # noqa: BLE001
                    title = ""
            c = candidate_from_note(h, title=title)
            if c:
                cands.append(c)
        return cands

    tasks = {asyncio.ensure_future(one_query(i, q)): ("q", i) for i, q in enumerate(queries)}
    tasks[asyncio.ensure_future(notes())] = ("notes", -1)
    budget = max(0.05, min(config.PER_QUERY_TIMEOUT_S, deadline - time.monotonic()))
    done, pending = await asyncio.wait(set(tasks), timeout=budget)
    partial = bool(pending)
    cancelled_queries = 0
    for t in pending:
        if tasks[t][0] == "q":
            cancelled_queries += 1
        t.cancel()
    if pending:
        # Let the cancellations actually land. Without this the tasks are
        # never awaited: their exceptions stay unretrieved and asyncio emits
        # "Task was destroyed but it is pending" at GC time.
        await asyncio.gather(*pending, return_exceptions=True)

    ranked: list[list[Candidate]] = [[] for _ in queries]
    per_hits = [0] * len(queries)
    warnings: list[str] = []
    note_cands: list[Candidate] = []
    failed_queries = 0
    for t in done:
        kind, qi = tasks[t]
        exc = t.exception()
        if exc is not None:
            if kind == "q":
                failed_queries += 1
                warnings.append(f"query_failed:{qi}")
                _LOG.warning("ask retrieve query %d failed: %r", qi, exc)
            else:
                warnings.append("notes_unavailable")
            continue
        if kind == "q":
            _, cands, w = t.result()
            ranked[qi] = cands
            per_hits[qi] = len(cands)
            warnings.extend(x for x in w if x not in warnings)
        else:
            note_cands = t.result()
    if partial:
        warnings.append("retrieve_partial")
    lists = ranked + [note_cands]
    weights = [1.0] * len(ranked) + [config.NOTE_WEIGHT]
    fused = rrf_fuse(lists, weights=weights)
    # Notes ride along as the last fusion list purely to get an RRF score;
    # they are not a sub-query, so they carry no hit_queries (and render no
    # "hit by: qN" line, and report [] in ask_sources).
    for c in fused:
        if c.source == "note":
            c.hit_queries = []
    # A query that was still pending at the deadline produced nothing, exactly
    # like one that raised: counting only exceptions made a hung Search report
    # "retrieve done hits=0" instead of an error.
    all_failed = bool(queries) and not fused and (failed_queries + cancelled_queries) == len(queries)
    return RetrieveResult(candidates=fused, per_query_hits=per_hits, warnings=warnings,
                          all_failed=all_failed, partial=partial)
