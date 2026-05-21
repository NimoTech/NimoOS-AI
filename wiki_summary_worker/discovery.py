"""Service URL discovery via /var/run/nimoos/*.url files.

Wiki and AI services write http://127.0.0.1:<random> to these files on
startup. This module reads them and returns the URLs.
"""
from __future__ import annotations
from pathlib import Path


class DiscoveryError(Exception):
    """Raised when a required service URL file is missing or unreadable.
    Worker treats this as a transient failure — break the round, retry next
    timer fire."""


_RUNTIME_DIR = Path("/var/run/nimoos")


def wiki_url() -> str:
    return _read(_RUNTIME_DIR / "wiki.url")


def ai_url() -> str:
    return _read(_RUNTIME_DIR / "ai.url")


def _read(p: Path) -> str:
    try:
        content = p.read_text().strip()
    except OSError as e:
        raise DiscoveryError(f"cannot read {p}: {e}") from e
    if not content.startswith("http://"):
        raise DiscoveryError(f"{p} contains unexpected content: {content!r}")
    return content
