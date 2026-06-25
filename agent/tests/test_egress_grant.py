"""
Tests for agent/egress/grant.py — egress-proxy grant-ticket client.

All tests use a real local stub HTTP server (threading.Thread + HTTPServer)
so that actual urllib socket paths are exercised without a live egress-proxy.

Coverage:
  - Happy path: stub returns 200 → register_grant returns True
  - Field contract: stub asserts host/max_bytes/ttl_sec/nonce present + correct types
  - Non-2xx: stub returns 500 → False (no exception)
  - Unreachable: connect to a port nothing is listening on → False (no exception)
  - Timeout: stub holds connection open past the 3-second window → False (no exception)
  - Nonce uniqueness: two calls produce different nonce values
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

import egress.grant as grant_mod
from egress.grant import register_grant


# ─── Stub server helpers ──────────────────────────────────────────────────────


class _GatherAndReply(BaseHTTPRequestHandler):
    """
    Simple stub handler.  Subclasses override ``_status`` / ``_delay``.
    Parsed request JSON is stashed on the class so tests can inspect it.
    """

    _status: int = 200
    _delay: float = 0.0
    last_body: dict[str, Any] = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).last_body = json.loads(raw)
        if self._delay:
            time.sleep(self._delay)
        self.send_response(self._status)
        self.end_headers()

    def log_message(self, *args: Any) -> None:  # silence output
        pass


def _make_handler(status: int = 200, delay: float = 0.0) -> type[_GatherAndReply]:
    return type(
        f"_Handler_{status}_{int(delay*1000)}ms",
        (_GatherAndReply,),
        {"_status": status, "_delay": delay},
    )


def _start_stub(handler_cls: type[_GatherAndReply]) -> tuple[HTTPServer, str]:
    """
    Start a stub server on a random free port.  Returns (server, base_url).
    The caller is responsible for shutting the server down.
    """
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def _free_port() -> int:
    """Return a port number that nothing is listening on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ─── Tests ────────────────────────────────────────────────────────────────────


class TestRegisterGrantHappyPath:
    def test_returns_true_on_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = _make_handler(200)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            result = register_grant("api.example.com:443", max_bytes=65536, ttl_sec=30)
        finally:
            server.shutdown()
        assert result is True

    def test_request_body_fields_present_and_typed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = _make_handler(200)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            register_grant("upload.example.com:443", max_bytes=1024, ttl_sec=60)
        finally:
            server.shutdown()

        body = handler.last_body
        assert "host" in body, "missing 'host' field"
        assert "max_bytes" in body, "missing 'max_bytes' field"
        assert "ttl_sec" in body, "missing 'ttl_sec' field"
        assert "nonce" in body, "missing 'nonce' field"

        assert isinstance(body["host"], str), "host must be str"
        assert isinstance(body["max_bytes"], int), "max_bytes must be int"
        assert isinstance(body["ttl_sec"], int), "ttl_sec must be int"
        assert isinstance(body["nonce"], str) and body["nonce"], "nonce must be non-empty str"

    def test_request_body_values_correct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = _make_handler(200)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            register_grant("cdn.example.com:443", max_bytes=999, ttl_sec=45)
        finally:
            server.shutdown()

        body = handler.last_body
        assert body["host"] == "cdn.example.com:443"
        assert body["max_bytes"] == 999
        assert body["ttl_sec"] == 45

    def test_default_ttl_sec_is_60(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = _make_handler(200)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            register_grant("s3.example.com:443", max_bytes=4096)
        finally:
            server.shutdown()

        assert handler.last_body["ttl_sec"] == 60


class TestRegisterGrantFailurePaths:
    def test_returns_false_on_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = _make_handler(500)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            result = register_grant("api.example.com:443", max_bytes=1024)
        finally:
            server.shutdown()
        assert result is False

    def test_returns_false_on_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = _make_handler(403)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            result = register_grant("api.example.com:443", max_bytes=1024)
        finally:
            server.shutdown()
        assert result is False

    def test_no_exception_on_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = _make_handler(500)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            # Must not raise
            try:
                register_grant("api.example.com:443", max_bytes=1024)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"register_grant raised on 500: {exc!r}")
        finally:
            server.shutdown()

    def test_returns_false_when_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = _free_port()
        monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", f"http://127.0.0.1:{port}")
        result = register_grant("api.example.com:443", max_bytes=1024)
        assert result is False

    def test_no_exception_when_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = _free_port()
        monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", f"http://127.0.0.1:{port}")
        try:
            register_grant("api.example.com:443", max_bytes=1024)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"register_grant raised on unreachable proxy: {exc!r}")

    def test_returns_false_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stub delays longer than the grant timeout
        handler = _make_handler(200, delay=5.0)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_TIMEOUT", "0.3")
            result = register_grant("api.example.com:443", max_bytes=1024)
        finally:
            server.shutdown()
        assert result is False

    def test_no_exception_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        handler = _make_handler(200, delay=5.0)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_TIMEOUT", "0.3")
            try:
                register_grant("api.example.com:443", max_bytes=1024)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"register_grant raised on timeout: {exc!r}")
        finally:
            server.shutdown()


class TestNonceUniqueness:
    def test_each_call_has_distinct_nonce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        nonces: list[str] = []

        class _CollectNonce(_GatherAndReply):
            _status = 200
            _delay = 0.0

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                body = json.loads(raw)
                nonces.append(body.get("nonce", ""))
                self.send_response(200)
                self.end_headers()

        server, url = _start_stub(_CollectNonce)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            register_grant("host1.example.com:443", max_bytes=100)
            register_grant("host2.example.com:443", max_bytes=200)
            register_grant("host3.example.com:443", max_bytes=300)
        finally:
            server.shutdown()

        assert len(nonces) == 3, f"expected 3 nonces, got {nonces}"
        assert len(set(nonces)) == 3, f"nonces not unique: {nonces}"
        for n in nonces:
            assert n, "nonce must be non-empty"


class TestEnvironmentConfig:
    def test_custom_grant_url_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify NIMOOS_EGRESS_GRANT_URL is honoured."""
        handler = _make_handler(200)
        server, url = _start_stub(handler)
        try:
            monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
            result = register_grant("env-test.example.com:443", max_bytes=512)
        finally:
            server.shutdown()
        assert result is True
        assert handler.last_body["host"] == "env-test.example.com:443"

    def test_2xx_range_all_succeed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """201 / 204 should also return True."""
        for status in (201, 204):
            handler = _make_handler(status)
            server, url = _start_stub(handler)
            try:
                monkeypatch.setenv("NIMOOS_EGRESS_GRANT_URL", url)
                result = register_grant("api.example.com:443", max_bytes=1024)
            finally:
                server.shutdown()
            assert result is True, f"expected True for HTTP {status}"
