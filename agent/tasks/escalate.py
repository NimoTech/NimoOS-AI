"""Escalation — forward an out-of-scope confirmation card to the task's
paired channel (spec §6 "范围外动作", acceptance scenario 4).

The TaskRunDriver used to answer every non-preauthorized card with an
immediate deny.  With a paired, button-capable channel configured as the
task's ``notify_channel``, the card is rendered there instead — Allow once /
Deny / Allow & add to the task's preauth — with a hard timeout after which
the answer is deny, exactly what an unattended run must fall back to.

Everything is injected for tests; production wiring supplies the live
``main._channel_manager`` through ``get_manager``.  Nothing here raises past
``escalate()``: every failure path resolves the confirmation as denied, so a
tool coroutine can never be left parked on ConfirmManager's 24h default.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("nimoos-agent.tasks")

# Spec §6: unanswered cards deny after 30 minutes (configurable).
DEFAULT_TIMEOUT_SECONDS = 1800

PERSIST_LABEL = "➕ Allow & add to pre-auth"

# Bound the free-text detail rendered into a chat card: `detail` can be a
# whole shell command straight from the model.
_DETAIL_MAX_CHARS = 400


def _timeout_seconds() -> float:
    try:
        v = float(os.environ.get("NIMOOS_TASK_CONFIRM_TIMEOUT", ""))
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return float(DEFAULT_TIMEOUT_SECONDS)


def _default_get_manager():
    try:
        import main  # noqa: PLC0415 — circular at module scope
    except Exception:  # noqa: BLE001
        return None
    return getattr(main, "_channel_manager", None)


def format_card(task_name: str, ev: dict, timeout_s: float) -> str:
    """The chat text for one escalated card.  English, like every other
    channel-facing string (see channels/router.py's MSG_* precedent)."""
    from .driver import _detail_of, _kind_of
    kind = _kind_of(ev)
    detail = (_detail_of(ev) or "")[:_DETAIL_MAX_CHARS]
    minutes = max(1, int(timeout_s // 60))
    lines = [f"⏳ Scheduled task '{task_name or '(unnamed)'}' requests approval:",
             f"{kind}: {detail}" if detail else str(kind),
             f"No response within {minutes} min = denied."]
    return "\n".join(lines)


def build(conn, task_row, *, session_id: str, confirm_mgr,
          get_manager=None, timeout: float | None = None):
    """Build the escalation callable for one run, or None.

    None means "this task cannot escalate" (no notify_channel configured),
    and the driver keeps its immediate-deny behavior.  Everything else that
    can go wrong (adapter down, unpaired chat, send failure) is decided
    per-card inside ``escalate``, because the channel can come and go during
    a 30-minute run.
    """
    try:
        raw_channel = str(task_row["notify_channel"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raw_channel = ""
    if not raw_channel:
        return None
    task_id = task_row["id"]
    user_id = task_row["user_id"]
    task_name = task_row["name"]
    get_manager = get_manager or _default_get_manager
    timeout_s = timeout if timeout is not None else _timeout_seconds()

    def _report(on_outcome, allow: bool, persist: bool) -> None:
        try:
            on_outcome(allow, persist)
        except Exception:  # noqa: BLE001 — bookkeeping must not break the run
            logger.warning("task escalate: on_outcome failed", exc_info=True)

    def _deny(cid: str, on_outcome) -> None:
        try:
            confirm_mgr.resolve(cid, False, expected_session_id=session_id,
                                source="task-driver")
        except Exception:  # noqa: BLE001 — expired/mismatched: already gone
            logger.warning("task escalate: deny-resolve failed for %s", cid,
                           exc_info=True)
        _report(on_outcome, False, False)

    def _persist(ev_kind: str, ev_detail: str) -> None:
        """Fold the approved action into the task's preauth.  Best-effort:
        a fold failure must not undo the allow that was already resolved.
        Re-reads the task so a preauth edited mid-run is not clobbered."""
        from . import preauth as _preauth
        from . import store as _store
        try:
            fresh = _store.get_task(conn, task_id, user_id)
            if fresh is None:
                return
            doc = _preauth.parse(fresh["preauth_json"])
            doc, bucket, entry = _preauth.fold_denied(
                doc, {"kind": ev_kind, "detail": ev_detail})
            doc, _report_unused = _preauth.parse_with_report(doc)
            if entry not in doc[bucket]:
                logger.warning("task escalate: preauth bucket %s full; "
                               "not persisting %r", bucket, entry)
                return
            _store.update_task(conn, task_id, user_id, preauth=doc)
            logger.info("task escalate: persisted %s rule into task %s",
                        bucket, task_id)
        except _preauth.FoldError as exc:
            logger.warning("task escalate: cannot persist approval (%s)",
                           exc.reason)
        except Exception:  # noqa: BLE001
            logger.warning("task escalate: persist failed", exc_info=True)

    async def escalate(ev: dict, on_outcome) -> None:
        from . import notify as _notify
        from .driver import _detail_of, _kind_of
        cid = str(ev.get("confirm_id") or "")
        if not cid:
            # Nothing is waiting on an id-less card; there is nothing to
            # resolve, only an outcome to record.
            _report(on_outcome, False, False)
            return
        try:
            target = _notify._resolve_target(
                conn, {"user_id": user_id}, raw_channel)
            manager = get_manager()
            if target is None or manager is None:
                _deny(cid, on_outcome)
                return
            instance_id, chat_id = target
            entry = (getattr(manager, "_running", None) or {}).get(instance_id)
            router = getattr(manager, "_router", None)
            if entry is None or router is None:
                _deny(cid, on_outcome)
                return
            adapter = entry[0]
            # `buttons_available` is Lark's runtime truth (consumer up);
            # adapters without the property are click-capable whenever they
            # run, hence the True default.
            if not getattr(adapter.capabilities, "supports_buttons", False) \
                    or not getattr(adapter, "buttons_available", True):
                _deny(cid, on_outcome)
                return

            ev_kind, ev_detail = _kind_of(ev), _detail_of(ev)

            def on_resolved(allow: bool, persist: bool) -> None:
                if allow and persist:
                    _persist(ev_kind, ev_detail)
                _report(on_outcome, allow, persist)

            ok = await router.surface_external_confirm(
                adapter, chat_id, session_id, cid,
                format_card(task_name, ev, timeout_s),
                timeout=timeout_s, persist_label=PERSIST_LABEL,
                on_resolved=on_resolved)
            if not ok:
                _deny(cid, on_outcome)
        except Exception:  # noqa: BLE001 — any surprise ends in a deny
            logger.warning("task escalate: escalation failed; denying",
                           exc_info=True)
            _deny(cid, on_outcome)

    return escalate
