"""
Tests for agent/egress/judge.py — local Ollama privacy judge.

All HTTP calls are monkeypatched; no real network connections.

Coverage:
  - Happy path: block / allow / ask verdicts returned from Ollama
  - Fail-safe: timeout, URLError, HTTP 500, bad JSON, unknown verdict → "ask"
  - Request contract: prompt contains host + truncated content, format=json, model used
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

import egress.judge as judge_mod
from egress.judge import judge


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_ollama_response(verdict: str, reason: str = "test") -> MagicMock:
    """Build a fake urllib response object returning the given verdict."""
    body = json.dumps(
        {"response": json.dumps({"verdict": verdict, "reason": reason})}
    ).encode("utf-8")
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_ollama_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://127.0.0.1:11434/api/generate",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# ─── Captured request helper ─────────────────────────────────────────────────

class _CapturingResponse:
    """Context-manager fake response that also captures the request sent."""

    def __init__(self, verdict: str):
        body = json.dumps(
            {"response": json.dumps({"verdict": verdict, "reason": "captured"})}
        ).encode("utf-8")
        self.status = 200
        self._body = body
        self.captured_req: urllib.request.Request | None = None

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


# ─── Verdict happy-path tests ─────────────────────────────────────────────────

class TestVerdictHappyPath:
    """Ollama returns well-formed JSON with a known verdict."""

    @pytest.mark.asyncio
    async def test_block_verdict(self):
        with patch("urllib.request.urlopen", return_value=_make_ollama_response("block")):
            result = await judge(b"some sensitive content", "evil.example.com")
        assert result == "block"

    @pytest.mark.asyncio
    async def test_allow_verdict(self):
        with patch("urllib.request.urlopen", return_value=_make_ollama_response("allow")):
            result = await judge(b"hello world", "example.com")
        assert result == "allow"

    @pytest.mark.asyncio
    async def test_ask_verdict(self):
        with patch("urllib.request.urlopen", return_value=_make_ollama_response("ask")):
            result = await judge(b"maybe sensitive?", "example.com")
        assert result == "ask"


# ─── Fail-safe tests ──────────────────────────────────────────────────────────

class TestFailSafe:
    """Any failure must return 'ask', never 'allow'."""

    @pytest.mark.asyncio
    async def test_socket_timeout_returns_ask(self):
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            result = await judge(b"content", "example.com")
        assert result == "ask"

    @pytest.mark.asyncio
    async def test_timeout_error_returns_ask(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = await judge(b"content", "example.com")
        assert result == "ask"

    @pytest.mark.asyncio
    async def test_url_error_connection_refused_returns_ask(self):
        cause = ConnectionRefusedError(111, "Connection refused")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(cause),
        ):
            result = await judge(b"content", "example.com")
        assert result == "ask"

    @pytest.mark.asyncio
    async def test_http_500_returns_ask(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=_make_ollama_http_error(500),
        ):
            result = await judge(b"content", "example.com")
        assert result == "ask"

    @pytest.mark.asyncio
    async def test_response_field_not_valid_json_returns_ask(self):
        """Ollama response field is not parseable JSON."""
        body = json.dumps({"response": "this is not json {{{"}).encode()
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            result = await judge(b"content", "example.com")
        assert result == "ask"

    @pytest.mark.asyncio
    async def test_unknown_verdict_value_returns_ask(self):
        """Model returns a valid JSON but verdict is not allow/block/ask."""
        body = json.dumps(
            {"response": json.dumps({"verdict": "maybe", "reason": "dunno"})}
        ).encode()
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            result = await judge(b"content", "example.com")
        assert result == "ask"

    @pytest.mark.asyncio
    async def test_empty_body_returns_ask(self):
        """Ollama returns empty body — json.loads fails."""
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b""
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            result = await judge(b"content", "example.com")
        assert result == "ask"

    @pytest.mark.asyncio
    async def test_missing_response_field_returns_ask(self):
        """Ollama JSON has no 'response' key — model_output parse gives empty string."""
        body = json.dumps({"other_field": "oops"}).encode()
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            result = await judge(b"content", "example.com")
        assert result == "ask"


# ─── Request contract tests ───────────────────────────────────────────────────

class TestRequestContract:
    """Verify that the outgoing request has the correct shape."""

    def _capture_request(self, verdict: str = "allow"):
        """Return a urlopen side_effect that stores the Request and returns a response."""
        captured: dict = {}

        def _fake_urlopen(req, timeout=None):
            captured["req"] = req
            captured["timeout"] = timeout
            body = json.dumps(
                {"response": json.dumps({"verdict": verdict, "reason": "ok"})}
            ).encode()
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = body
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        return _fake_urlopen, captured

    @pytest.mark.asyncio
    async def test_prompt_contains_host(self):
        fake_urlopen, captured = self._capture_request()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await judge(b"hello", "target.example.com")

        req: urllib.request.Request = captured["req"]
        body = json.loads(req.data)
        assert "target.example.com" in body["prompt"]

    @pytest.mark.asyncio
    async def test_prompt_contains_content(self):
        fake_urlopen, captured = self._capture_request()
        content = b"super secret password"
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await judge(content, "example.com")

        req: urllib.request.Request = captured["req"]
        body = json.loads(req.data)
        assert "super secret password" in body["prompt"]

    @pytest.mark.asyncio
    async def test_format_is_json(self):
        fake_urlopen, captured = self._capture_request()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await judge(b"data", "example.com")

        req: urllib.request.Request = captured["req"]
        body = json.loads(req.data)
        assert body["format"] == "json"

    @pytest.mark.asyncio
    async def test_stream_is_false(self):
        fake_urlopen, captured = self._capture_request()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await judge(b"data", "example.com")

        req: urllib.request.Request = captured["req"]
        body = json.loads(req.data)
        assert body["stream"] is False

    @pytest.mark.asyncio
    async def test_model_is_configured(self, monkeypatch):
        monkeypatch.setenv("NIMOOS_EGRESS_JUDGE_MODEL", "llama3:8b")
        # Reload config at call time (env-driven)
        fake_urlopen, captured = self._capture_request()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await judge(b"data", "example.com")

        req: urllib.request.Request = captured["req"]
        body = json.loads(req.data)
        assert body["model"] == "llama3:8b"

    @pytest.mark.asyncio
    async def test_content_is_truncated_to_maxbytes(self, monkeypatch):
        monkeypatch.setenv("NIMOOS_EGRESS_JUDGE_MAXBYTES", "10")
        fake_urlopen, captured = self._capture_request()
        long_content = b"A" * 100
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await judge(long_content, "example.com")

        req: urllib.request.Request = captured["req"]
        body = json.loads(req.data)
        # Prompt should contain only 10 A's worth of content, not 100
        prompt: str = body["prompt"]
        # 10 A's appears; 20 A's (double the limit) should NOT appear
        assert "A" * 10 in prompt
        assert "A" * 11 not in prompt

    @pytest.mark.asyncio
    async def test_request_url_uses_ollama_url_env(self, monkeypatch):
        monkeypatch.setenv("NIMOOS_OLLAMA_URL", "http://192.168.1.100:11434")
        fake_urlopen, captured = self._capture_request()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await judge(b"data", "example.com")

        req: urllib.request.Request = captured["req"]
        assert req.full_url == "http://192.168.1.100:11434/api/generate"

    @pytest.mark.asyncio
    async def test_timeout_is_applied(self, monkeypatch):
        monkeypatch.setenv("NIMOOS_EGRESS_JUDGE_TIMEOUT", "5.0")
        fake_urlopen, captured = self._capture_request()
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            await judge(b"data", "example.com")

        assert captured["timeout"] == pytest.approx(5.0)
