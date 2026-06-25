"""
egress/grant.py — Grant-ticket client for the egress-proxy control server.

register_grant(host: str, max_bytes: int, ttl_sec: int = 60) -> bool
    Issues a POST to the egress-proxy grant control endpoint, registering a
    byte-budget ticket for *host*.  Returns True on HTTP 2xx, False on any
    failure (non-2xx, timeout, connection error).  Never raises.

    The grant only pre-authorises the byte budget in the proxy; the proxy's
    per-connection byte gate still enforces the limit.  A False return means
    the proxy will fall back to its confirm/block flow — this is the safe
    direction, so callers must not abort on False.

Environment variables:
    NIMOOS_EGRESS_GRANT_URL     default: http://127.0.0.1:8889
    NIMOOS_EGRESS_GRANT_TIMEOUT default: 3.0  (seconds)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger("nimoos-agent")

# ─── Config ───────────────────────────────────────────────────────────────────

_DEFAULT_GRANT_URL = "http://127.0.0.1:8889"
_DEFAULT_TIMEOUT = 3.0


def _grant_url() -> str:
    return os.environ.get("NIMOOS_EGRESS_GRANT_URL", _DEFAULT_GRANT_URL).rstrip("/")


def _timeout() -> float:
    try:
        return float(os.environ.get("NIMOOS_EGRESS_GRANT_TIMEOUT", _DEFAULT_TIMEOUT))
    except (ValueError, TypeError):
        return _DEFAULT_TIMEOUT


# ─── Public API ───────────────────────────────────────────────────────────────


def register_grant(host: str, max_bytes: int, ttl_sec: int = 60) -> bool:
    """
    Register a byte-budget grant ticket with the egress-proxy.

    Args:
        host:      Target hostname (e.g. "api.example.com:443").
        max_bytes: Maximum bytes the proxy will forward under this grant.
        ttl_sec:   Ticket lifetime in seconds (default 60).

    Returns:
        True  — proxy acknowledged the grant (HTTP 2xx).
        False — proxy unreachable, returned non-2xx, or request timed out.
                The caller should NOT raise on False; the proxy's byte gate
                still enforces safety.
    """
    body: dict = {
        "host": host,
        "max_bytes": max_bytes,
        "ttl_sec": ttl_sec,
        "nonce": uuid.uuid4().hex,
    }
    payload = json.dumps(body).encode("utf-8")
    url = f"{_grant_url()}/grant"
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            status: int = resp.status
            if 200 <= status < 300:
                return True
            logger.warning(
                "egress.grant: non-2xx response %d from %s for host=%s",
                status,
                url,
                host,
            )
            return False
    except urllib.error.HTTPError as exc:
        logger.warning(
            "egress.grant: HTTP error %d from %s for host=%s: %s",
            exc.code,
            url,
            host,
            exc,
        )
        return False
    except TimeoutError as exc:
        logger.warning(
            "egress.grant: timeout reaching %s for host=%s: %s",
            url,
            host,
            exc,
        )
        return False
    except OSError as exc:
        # Covers ConnectionRefusedError, urllib.error.URLError, socket errors, etc.
        logger.warning(
            "egress.grant: connection error reaching %s for host=%s: %s",
            url,
            host,
            exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — absolute last-resort guard
        logger.warning(
            "egress.grant: unexpected error registering grant for host=%s: %s",
            host,
            exc,
        )
        return False
