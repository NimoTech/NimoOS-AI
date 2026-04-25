import asyncio
import sqlite3
import time

DEFAULT_TIMEOUT = 300  # 5 minutes


class ConfirmManager:
    def __init__(self, db: sqlite3.Connection, timeout: float = DEFAULT_TIMEOUT):
        self._db = db
        self._timeout = timeout
        self._pending: dict[str, asyncio.Event] = {}
        self._results: dict[str, bool] = {}

    async def wait(self, session_id: str, action: str, description: str, command: str) -> bool:
        """Pause agent until user confirms or timeout expires. Returns True if confirmed."""
        self._db.execute(
            "INSERT OR REPLACE INTO pending_confirmations (session_id, action, description, command, created_at) VALUES (?,?,?,?,?)",
            (session_id, action, description, command, int(time.time())),
        )
        self._db.commit()

        event = asyncio.Event()
        self._pending[session_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=self._timeout)
        except asyncio.TimeoutError:
            self._cleanup(session_id)
            return False

        confirmed = self._results.pop(session_id, False)
        self._cleanup(session_id)
        return confirmed

    def resolve(self, session_id: str, confirmed: bool) -> None:
        """Called by the /confirm or /cancel endpoint to unblock wait()."""
        if session_id not in self._pending:
            raise KeyError("session_expired")
        self._results[session_id] = confirmed
        self._pending[session_id].set()

    def _cleanup(self, session_id: str) -> None:
        self._pending.pop(session_id, None)
        self._db.execute("DELETE FROM pending_confirmations WHERE session_id=?", (session_id,))
        self._db.commit()
