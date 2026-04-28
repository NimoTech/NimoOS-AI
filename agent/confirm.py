import asyncio
import sqlite3
import time
import uuid

# 24 hours. Long enough that a user who closed the tab and came back the next
# day can still approve. Server restart wipes _pending anyway, so very long
# timeouts don't pin process memory across realistic operational windows.
DEFAULT_TIMEOUT = 24 * 60 * 60


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

    def resolve(self, confirm_id: str, confirmed: bool, expected_session_id: str | None = None) -> None:
        """Unblock the wait() corresponding to confirm_id.

        expected_session_id, if provided, must match the registered session;
        otherwise raises KeyError. Defends against stale or mis-routed POSTs.
        """
        pending = self._pending.get(confirm_id)
        if pending is None:
            raise KeyError("confirm_expired")
        if expected_session_id is not None and pending.session_id != expected_session_id:
            raise KeyError("confirm_session_mismatch")
        self._results[confirm_id] = confirmed
        pending.event.set()

    def cancel_session(self, session_id: str) -> int:
        """Reject every pending confirmation for the given session.
        Returns count of confirmations cancelled. Does not raise if there are none."""
        ids = [cid for cid, p in self._pending.items() if p.session_id == session_id]
        for cid in ids:
            self._results[cid] = False
            self._pending[cid].event.set()
        return len(ids)

    def _cleanup(self, confirm_id: str) -> None:
        self._pending.pop(confirm_id, None)
        self._db.execute(
            "DELETE FROM pending_confirmations WHERE confirm_id=?",
            (confirm_id,),
        )
        self._db.commit()
