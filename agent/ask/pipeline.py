"""Orchestrates the ask pipeline and emits its SSE events (spec §3.3.5, §4)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ask import config, evidence, retrieve, rewrite, store
from ask.prompt import STEP_SUMMARY_INSTRUCTION

_LOG = logging.getLogger(__name__)


@dataclass
class AskResult:
    evidence_block: str = ""
    plan: dict = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    stages: list[dict] = field(default_factory=list)
    dropped: int = 0
    warnings: list[str] = field(default_factory=list)


class _Stages:
    def __init__(self, sink):
        self.sink, self.records, self._t0 = sink, [], {}

    async def start(self, stage: str):
        self._t0[stage] = time.monotonic()
        rec = {"stage": stage, "status": "start", "ms": 0, "detail": ""}
        # Recorded, not just streamed: "answer" has no end() (the model call is
        # not part of this pipeline), so without this the persisted stages of
        # an ask_turn stopped at "pack" and a replay could not show that the
        # pipeline actually handed off to the model.
        self.records.append(rec)
        await self.sink.put({"type": "ask_stage", **rec})

    async def end(self, stage: str, status: str, detail: str = ""):
        ms = int((time.monotonic() - self._t0.get(stage, time.monotonic())) * 1000)
        rec = {"stage": stage, "status": status, "ms": ms, "detail": detail}
        self.records.append(rec)
        await self.sink.put({"type": "ask_stage", **rec})


def _no_evidence_note(plan: rewrite.Plan, reason: str) -> str:
    """Server-authored, unfenced (i.e. trusted) replacement for an empty
    evidence pack. The prompt tells the model to say which queries were
    tried; with nothing in the user turn about the retrieval it had no way
    to do that and would invent an explanation (spec §5 row 7)."""
    tried = " ".join(f"{i + 1}) {q.q}" for i, q in enumerate(plan.queries))
    return (f"Server-side retrieval ran {len(plan.queries)} queries and returned no usable "
            f"passages. Queries tried: {tried}. Reason: {reason}.")


def _plan_event(plan: rewrite.Plan, hits: list[int] | None) -> dict:
    payload = plan.to_payload()
    for i, q in enumerate(payload["queries"]):
        q["hits"] = hits[i] if hits and i < len(hits) else 0
    return {"type": "ask_plan", **payload, "fallback": plan.fallback,
            "needs_retrieval": plan.needs_retrieval}


async def _step_summaries(plan, pack, complete, *, pool_chars: int, deadline: float) -> list[str]:
    """One-line-per-sub-query digests, prepended to the evidence pack.

    Bounded three ways, because the pack itself must never be at risk: the
    gate looks at the PRE-budget candidate pool (pack.total_chars is
    post-budget and so always <= budget — testing it meant "on for every
    list/compare/aggregate question"), the calls run concurrently, and each
    one is capped so a hung background model costs one timeout instead of
    the whole run.
    """
    mode = config.step_summary_mode()
    if complete is None or mode == "off":
        return []
    big = pool_chars > config.EVIDENCE_BUDGET_CHARS * 1.5
    if mode == "auto" and not (big and plan.intent in ("list", "compare", "aggregate")):
        return []
    remaining = deadline - time.monotonic() - config.STEP_SUMMARY_RESERVE_S
    if remaining < config.STEP_SUMMARY_MIN_REMAINING_S:
        return []
    per_call = min(config.STEP_SUMMARY_TIMEOUT_S, remaining)

    async def one(qi: int, q) -> str:
        items = [f"[{n}] {c.text[:600]}" for n, c in enumerate(pack.items, 1) if qi in c.hit_queries]
        if not items:
            return ""
        try:
            s = await asyncio.wait_for(
                complete(STEP_SUMMARY_INSTRUCTION, f"Query: {q.q}\n\n" + "\n\n".join(items),
                         max_tokens=config.STEP_SUMMARY_MAX_TOKENS, timeout=per_call),
                timeout=per_call)
        except Exception:  # noqa: BLE001 — this query gets no summary, nothing else changes
            return ""
        return f"{q.q}: {s.strip()}" if s and s.strip() else ""

    got = await asyncio.gather(*(one(qi, q) for qi, q in enumerate(plan.queries)),
                               return_exceptions=True)
    return [g for g in got if isinstance(g, str) and g]


def notes_exclude_prefixes(conn) -> tuple[str, ...]:
    """Directory prefixes retrieve() must drop from document hits: the notes
    layer's root (agent/notes/store.py). Its .md files (plus log.md/index.md)
    are Parser-indexed like any other folder, but the same notes already reach
    the pack through the notes collection, so as documents they only crowd
    out real sources and leak earlier answers back into later questions."""
    from notes import store as notes_store  # noqa: PLC0415 — keep ask importable without notes deps
    root = str(notes_store.get_notes_root(conn) or "").rstrip("/")
    return (root + "/",) if root else ()


async def run(*, question: str, session_id: str, user_id: str, run_id: str, complete, sink, conn,
              search, parser, window_tokens: int | None = None,
              include_draft_notes: bool = False,
              exclude_prefixes: tuple[str, ...] = ()) -> AskResult:
    deadline = time.monotonic() + config.PIPELINE_TIMEOUT_S
    st = _Stages(sink)
    result = AskResult()

    # 1 rewrite
    await st.start("rewrite")
    hint = ""
    try:
        hint = store.recent_questions(conn, session_id)
    except Exception:  # noqa: BLE001
        hint = ""
    plan = await rewrite.rewrite(question, complete=complete, history_hint=hint)
    result.plan = plan.to_payload()
    await sink.put(_plan_event(plan, None))
    await st.end("rewrite", "fallback" if plan.fallback else "done",
                 f"intent={plan.intent} queries={len(plan.queries)}")

    if not plan.needs_retrieval:
        for s in ("retrieve", "rank", "pack"):
            await st.end(s, "skipped")
        await sink.put({"type": "ask_sources", "items": [], "dropped": 0, "warnings": []})
        await st.start("answer")
        result.stages = st.records
        _persist(conn, session_id, run_id, question, result)
        return result

    # 2 retrieve
    await st.start("retrieve")
    rr = await retrieve.retrieve(plan, question=question, user_id=user_id, search=search, parser=parser,
                                 deadline=deadline, include_draft_notes=include_draft_notes,
                                 note_title=lambda nid: store.note_title(conn, user_id, nid),
                                 exclude_prefixes=exclude_prefixes)
    result.warnings.extend(rr.warnings)
    await sink.put(_plan_event(plan, rr.per_query_hits))
    if rr.all_failed:
        result.warnings.append("retrieve_failed")
        result.evidence_block = _no_evidence_note(plan, "retrieve_error")
        await st.end("retrieve", "error", "all queries failed")
        for s in ("rank", "pack"):
            await st.end(s, "skipped")
        await sink.put({"type": "ask_sources", "items": [], "dropped": 0, "warnings": result.warnings})
        await st.start("answer")
        result.stages = st.records
        _persist(conn, session_id, run_id, question, result)
        return result
    await st.end("retrieve", "done", f"hits={sum(rr.per_query_hits)} unique={len(rr.candidates)}"
                 + (" partial" if rr.partial else ""))

    # 3 rank (fusion done in retrieve; MECE here)
    await st.start("rank")
    try:
        seen = store.seen_keys(conn, session_id)
    except Exception:  # noqa: BLE001
        seen = set()
    ranked = retrieve.apply_mece(rr.candidates, seen)
    # Pre-budget pool size: what the step-summary gate must look at.
    pool_chars = sum(len(c.text) for c in ranked)
    await st.end("rank", "done", f"kept={len(ranked)} seen_excluded={len(rr.candidates) - len(ranked)}")

    # 4 pack
    await st.start("pack")
    budget = config.budget_for_window(window_tokens)
    # Merging, parent expansion and inlining each cost a Search round trip per
    # group, so they only run over what could plausibly be packed: at most
    # twice the item cap. Everything below that is counted as dropped, not
    # processed.
    head, tail = ranked[:config.EVIDENCE_MAX_ITEMS * 2], ranked[config.EVIDENCE_MAX_ITEMS * 2:]
    pack = await evidence.build_pack(head, invoke_tool=search.invoke_tool, user_id=user_id,
                                     max_items=config.EVIDENCE_MAX_ITEMS, budget_chars=budget)
    pack.dropped += len(tail)          # ask_sources.dropped covers everything not sent
    pack.step_summaries = await _step_summaries(plan, pack, complete,
                                                pool_chars=pool_chars, deadline=deadline)
    result.sources = evidence.sources_payload(pack, total_queries=len(plan.queries))
    result.dropped = pack.dropped
    result.evidence_block = evidence.render_pack(pack, budget_chars=budget)
    if not pack.items:
        result.evidence_block = _no_evidence_note(
            plan, "partial_timeout" if rr.partial else "no_hits")
    await st.end("pack", "done", f"items={len(pack.items)} chars={pack.total_chars} dropped={pack.dropped}")
    await sink.put({"type": "ask_sources", "items": result.sources, "dropped": pack.dropped,
                    "warnings": result.warnings})
    await st.start("answer")
    result.stages = st.records
    _persist(conn, session_id, run_id, question, result)
    return result


def _persist(conn, session_id, run_id, question, result: AskResult) -> None:
    try:
        store.insert_turn(conn, session_id=session_id, run_id=run_id, question=question,
                          plan=result.plan, sources=result.sources, stages=result.stages)
    except Exception:  # noqa: BLE001 — persistence must not break the answer
        _LOG.warning("ask: failed to persist ask_turn for %s", session_id, exc_info=True)


async def run_guarded(**kwargs) -> AskResult:
    sink = kwargs.get("sink")
    try:
        return await asyncio.wait_for(run(**kwargs), timeout=config.PIPELINE_TIMEOUT_S + 5)
    except Exception as exc:  # noqa: BLE001 — the model still runs without evidence
        _LOG.warning("ask pipeline failed: %r", exc, exc_info=True)
        if sink is not None:
            try:
                await sink.put({"type": "ask_stage", "stage": "retrieve", "status": "error", "ms": 0,
                                "detail": f"pipeline error: {type(exc).__name__}"})
                # The UI's stage timeline waits for every stage to resolve;
                # without these two it would sit on "rank"/"pack" forever.
                for stage in ("rank", "pack"):
                    await sink.put({"type": "ask_stage", "stage": stage, "status": "skipped",
                                    "ms": 0, "detail": ""})
                await sink.put({"type": "ask_sources", "items": [], "dropped": 0,
                                "warnings": ["pipeline_error"]})
            except Exception:  # noqa: BLE001
                pass
        return AskResult(warnings=["pipeline_error"])
