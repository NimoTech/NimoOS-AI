from __future__ import annotations
import time
import pytest
from wiki_summary_worker.rate_limit import RateLimiter, RateLimitExceeded


def test_first_call_passes_through(tmp_path, monkeypatch):
    monkeypatch.setattr(RateLimiter, "PATH", tmp_path / "calls.log")
    r = RateLimiter()
    r.take_or_die(max_per_hour=10)
    assert (tmp_path / "calls.log").exists()


def test_exceeds_limit_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(RateLimiter, "PATH", tmp_path / "calls.log")
    r = RateLimiter()
    for _ in range(5):
        r.take_or_die(5)
    with pytest.raises(RateLimitExceeded):
        r.take_or_die(5)


def test_old_entries_evicted(tmp_path, monkeypatch):
    monkeypatch.setattr(RateLimiter, "PATH", tmp_path / "calls.log")
    old_ms = int((time.time() - 7200) * 1000)
    (tmp_path / "calls.log").write_text("\n".join(str(old_ms) for _ in range(5)))
    r = RateLimiter()
    r.take_or_die(max_per_hour=5)


def test_malformed_lines_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(RateLimiter, "PATH", tmp_path / "calls.log")
    (tmp_path / "calls.log").write_text("not-a-number\n123\n\nanother-junk\n")
    r = RateLimiter()
    r.take_or_die(max_per_hour=10)
