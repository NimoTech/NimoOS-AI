import asyncio
import sqlite3
import time
import uuid

from audit import audit as _audit

# 24 hours. Long enough that a user who closed the tab and came back the next
# day can still approve. Server restart wipes _pending anyway, so very long
# timeouts don't pin process memory across realistic operational windows.
DEFAULT_TIMEOUT = 24 * 60 * 60

# The spec's three elicitation outcomes. `decline` is "the user said no"; `cancel` is
# "the user never acted" (timeout, session cancelled, card lost). They mean different
# things to a server, so we never collapse them into one.
ELICIT_ACTIONS = ("accept", "decline", "cancel")


class _Pending:
    __slots__ = ("event", "session_id")

    def __init__(self, session_id: str):
        self.event = asyncio.Event()
        self.session_id = session_id


class ConfirmManager:
    """Coordinates user-facing confirmations.

    Each confirmation gets a unique `confirm_id`, so a single session can have
    multiple confirms in flight (e.g. parallel tool calls in one assistant turn)
    without races.

    Usage from a tool:
        confirm_id = mgr.register(session_id, action, description, command)
        await queue.put({"type": "confirmation_required", "confirm_id": confirm_id, ...})
        confirmed = await mgr.wait(confirm_id)
    """

    def __init__(self, db: sqlite3.Connection, timeout: float = DEFAULT_TIMEOUT):
        self._db = db
        self._timeout = timeout
        self._pending: dict[str, _Pending] = {}
        self._results: dict[str, bool] = {}
        self._remember: dict[str, bool] = {}
        # Elicitation only. Both are IN-MEMORY AND READ-ONCE by design: the answer to a
        # form question must never reach event_log, pending_confirmations, or the audit
        # trail. The spec forbids servers from asking for passwords / API keys / tokens
        # through form mode — a non-compliant one is exactly the threat this defends
        # against, and everything else in this class is durable storage.
        self._actions: dict[str, str] = {}
        self._contents: dict[str, dict] = {}

    def register(self, session_id: str, action: str, description: str, command: str) -> str:
        """Allocate a confirm_id and an Event so a /confirm POST that arrives
        before wait() is reached still resolves correctly. Caller should emit
        the SSE event with the returned id, then await wait()."""
        confirm_id = str(uuid.uuid4())
        self._pending[confirm_id] = _Pending(session_id)
        self._db.execute(
            "INSERT INTO pending_confirmations "
            "(confirm_id, session_id, action, description, command, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (confirm_id, session_id, action, description, command, int(time.time())),
        )
        self._db.commit()
        return confirm_id

    async def wait(self, confirm_id: str) -> bool:
        """Block until /confirm or /cancel resolves this id, or timeout fires."""
        pending = self._pending.get(confirm_id)
        if pending is None:
            return False
        try:
            await asyncio.wait_for(pending.event.wait(), timeout=self._timeout)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        finally:
            self._cleanup(confirm_id)

        return self._results.pop(confirm_id, False)

    async def wait_elicit(self, confirm_id: str, *, timeout: float | None = None,
                          on_timeout: str = "cancel") -> tuple[str, dict | None]:
        """Three-state sibling of wait(), for MCP elicitation.

        Returns (action, content). Anything that is not an explicit user choice —
        timeout, cancel_session, an id we never registered — maps to "cancel", the
        spec's word for "the user did not act on this", as opposed to "decline" =
        "the user said no".

        There is no SDK-side deadline on the elicitation callback (the MRTR driver's
        _dispatch_all awaits it unbounded), so the timeout here is the real one.
        That is intentional: the spec wants the user to be able to walk away,
        authorize out of band, and come back.

        `timeout` overrides the manager-wide DEFAULT_TIMEOUT for THIS wait only.
        A URL authorization card cannot use the 24h default: it holds a server's
        `requestState`, which the spec advises servers to give a SHORT TTL and to
        validate on arrival. Sending `accept` a day later means sending it against
        expired state — strictly worse than sending it in three minutes. Form cards
        keep the 24h default (nothing there expires).

        `on_timeout` is what a timeout MEANS to this caller, and it exists for the
        same reason. For a form card a timeout is "cancel": we have no answer, so
        there is nothing to submit. For a URL card a timeout is "accept": the user
        already consented to open the page, `accept` per spec asserts ONLY that
        consent ("It does not mean that the interaction is complete"), so it stays
        true whether or not they finished — and sending it gives a long-polling or
        state-only server its chance instead of failing the call outright. An
        explicit user answer always wins; this only fires on a real timeout.
        """
        if on_timeout not in ELICIT_ACTIONS:
            raise ValueError(f"unknown elicitation action: {on_timeout!r}")
        pending = self._pending.get(confirm_id)
        if pending is None:
            return ("cancel", None)
        try:
            try:
                await asyncio.wait_for(
                    pending.event.wait(),
                    timeout=self._timeout if timeout is None else timeout)
            except asyncio.TimeoutError:
                # content is unconditionally None: a timeout means no answer exists,
                # whatever we map the ACTION to.
                return (on_timeout, None)
            finally:
                self._cleanup(confirm_id)
                self._results.pop(confirm_id, None)
            # DEVIATION FROM PLAN, empirically forced: asyncio.wait_for swallows a
            # CancelledError that arrives after its wrapped future is already done
            # (cpython asyncio/tasks.py wait_for: `except CancelledError: if
            # fut.done(): return fut.result()`) — verified directly against this
            # venv's 3.11.2 interpreter. That happens here whenever resolve() ran
            # before the waiter got its first chance to run: pending.event.wait()
            # is already complete, so a cancel() landing right after does not
            # propagate on its own, contradicting the "CancelledError 天然向上传播"
            # assumption in the plan's own note. Task.cancelling() (3.11+) is the
            # sanctioned way to detect a swallowed cancellation and re-raise it.
            # CORRECTION RECORD (do not "fix" this back): the plan's inline note
            # asserting natural propagation is simply wrong for this one path —
            # confirmed by code review (task-3-report.md, fix round 2) — so this
            # guard is required, not optional polish. Its behaviour under a real
            # anyio task group (the shape Task 4's SDK callback actually runs in,
            # where Task.cancelling() being a whole-task counter could in
            # principle misfire on an unrelated outer cancellation) is pinned by
            # tests/test_confirm_elicit.py::test_wait_elicit_completes_normally_inside_a_live_anyio_task_group
            # and ::test_wait_elicit_cancelled_by_a_task_group_scope_propagates_and_clears_memory.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError()
            return (self._actions.get(confirm_id, "cancel"),
                    self._contents.get(confirm_id))
        finally:
            # Read-once on EVERY path, cancellation included: an answer left behind
            # here is an answer sitting in process memory for no reason.
            self._actions.pop(confirm_id, None)
            self._contents.pop(confirm_id, None)

    def resolve(self, confirm_id: str, confirmed: bool, remember: bool = False,
                expected_session_id: str | None = None, *,
                action: str | None = None, content: dict | None = None) -> None:
        """Unblock the wait() / wait_elicit() corresponding to confirm_id.

        expected_session_id, if provided, must match the registered session;
        otherwise raises KeyError. Defends against stale or mis-routed POSTs.

        `action` / `content` are the elicitation extension. Existing two-state callers
        pass neither and are completely unaffected. `content` is held in memory only —
        see the _contents comment in __init__.
        """
        if action is not None and action not in ELICIT_ACTIONS:
            raise ValueError(f"unknown elicitation action: {action!r}")
        pending = self._pending.get(confirm_id)
        if pending is None:
            raise KeyError("confirm_expired")
        if expected_session_id is not None and pending.session_id != expected_session_id:
            raise KeyError("confirm_session_mismatch")
        try:
            row = self._db.execute(
                "SELECT action, command FROM pending_confirmations WHERE confirm_id=?",
                (confirm_id,)).fetchone()
            # NOTE: `content` is deliberately absent from this call and must stay
            # absent. The audit trail is long-lived on disk; the user's answer is not
            # allowed there. `decision` carries the three-state outcome instead.
            _audit("confirm_resolved", session_id=pending.session_id,
                   confirm_id=confirm_id,
                   action=(row["action"] if row else None),
                   command=(row["command"] if row else None),
                   decision=(action if action is not None
                             else ("approved" if confirmed else "denied")),
                   remember=bool(remember))
        except Exception:  # noqa: BLE001 — audit must not break resolution
            pass
        self._results[confirm_id] = confirmed
        if action is not None:
            self._actions[confirm_id] = action
            if content is not None:
                self._contents[confirm_id] = content
        if remember:
            self._remember[confirm_id] = True
        pending.event.set()

    def cancel_session(self, session_id: str) -> int:
        """Reject every pending confirmation for the given session.
        Returns count of confirmations cancelled. Does not raise if there are none."""
        ids = [cid for cid, p in self._pending.items() if p.session_id == session_id]
        for cid in ids:
            self._results[cid] = False
            self._pending[cid].event.set()
        return len(ids)

    def consume_remember(self, confirm_id: str) -> bool:
        """Read-once: whether the user ticked 'remember' for this confirm.
        Safe to call after wait() (wait's _cleanup does not touch _remember)."""
        return self._remember.pop(confirm_id, False)

    def _cleanup(self, confirm_id: str) -> None:
        self._pending.pop(confirm_id, None)
        self._db.execute(
            "DELETE FROM pending_confirmations WHERE confirm_id=?",
            (confirm_id,),
        )
        self._db.commit()
