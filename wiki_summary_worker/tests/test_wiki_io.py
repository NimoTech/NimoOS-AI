from __future__ import annotations
import httpx
import pytest
from wiki_summary_worker import wiki_io


def _client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="http://wiki.test")


def test_fetch_needs_summary_parses_nodes(monkeypatch):
    seen = {}
    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={
            "nodes": [
                {"path": "/a", "level": "project", "last_modified_ms": 100,
                 "current_ai_label": "", "child_count": 5},
            ]
        })
    monkeypatch.setattr(wiki_io, "_make_client", lambda: _client(handler))
    monkeypatch.setattr(wiki_io.discovery, "wiki_url", lambda: "http://wiki.test")

    rows = wiki_io.fetch_needs_summary(limit=10)
    assert "/v1/wiki/_internal/needs-summary" in seen["url"]
    assert "limit=10" in seen["url"]
    assert len(rows) == 1
    assert rows[0]["path"] == "/a"
    assert rows[0]["last_modified_ms"] == 100


def test_fetch_needs_summary_empty(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"nodes": []})
    monkeypatch.setattr(wiki_io, "_make_client", lambda: _client(handler))
    monkeypatch.setattr(wiki_io.discovery, "wiki_url", lambda: "http://wiki.test")
    assert wiki_io.fetch_needs_summary(10) == []


def test_fetch_node_evidence_parses_shape(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={
            "node_path": "/x",
            "child_map": [{"name": "a.md", "size": 100, "is_dir": False, "ext": "md"}],
            "text_files": [{"path": "/x/a.md", "size": 100, "mtime_ms": 1, "ext": "md"}],
            "pdf_files": [],
            "skipped_sample": [],
        })
    monkeypatch.setattr(wiki_io, "_make_client", lambda: _client(handler))
    monkeypatch.setattr(wiki_io.discovery, "wiki_url", lambda: "http://wiki.test")

    e = wiki_io.fetch_node_evidence(path="/x", text_limit=10, pdf_limit=5)
    assert e["node_path"] == "/x"
    assert e["text_files"][0]["path"] == "/x/a.md"


def test_post_summary_sends_body(monkeypatch):
    captured = {}
    def handler(req):
        import json as _json
        captured["body"] = _json.loads(req.content)
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(wiki_io, "_make_client", lambda: _client(handler))
    monkeypatch.setattr(wiki_io.discovery, "wiki_url", lambda: "http://wiki.test")

    wiki_io.post_summary(
        path="/x", ai_label="L", summary="S",
        based_on_last_modified_ms=42, generator_version="v",
    )
    assert captured["body"] == {
        "path": "/x", "ai_label": "L", "summary": "S",
        "based_on_last_modified_ms": 42, "generator_version": "v",
    }


def test_post_summary_raises_on_4xx(monkeypatch):
    def handler(req):
        return httpx.Response(404, text="not found")
    monkeypatch.setattr(wiki_io, "_make_client", lambda: _client(handler))
    monkeypatch.setattr(wiki_io.discovery, "wiki_url", lambda: "http://wiki.test")
    with pytest.raises(httpx.HTTPError):
        wiki_io.post_summary(
            path="/x", ai_label="L", summary="S",
            based_on_last_modified_ms=42, generator_version="v",
        )
