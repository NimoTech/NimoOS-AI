from __future__ import annotations
import pytest
from wiki_summary_worker import sampler
from wiki_summary_worker.config import Config


def test_gather_reads_text_files(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("Hello world from a.md")
    (tmp_path / "b.txt").write_text("body of b")

    def fake_evidence(*, path, text_limit, pdf_limit):
        return {
            "node_path": str(tmp_path),
            "child_map": [{"name": "a.md", "size": 21, "is_dir": False, "ext": "md"}],
            "text_files": [
                {"path": str(tmp_path / "a.md"), "size": 21, "mtime_ms": 1, "ext": "md"},
                {"path": str(tmp_path / "b.txt"), "size": 9, "mtime_ms": 1, "ext": "txt"},
            ],
            "pdf_files": [],
            "skipped_sample": [],
        }
    monkeypatch.setattr(sampler.wiki_io, "fetch_node_evidence", fake_evidence)

    cfg = Config()
    ev = sampler.gather(str(tmp_path), cfg)
    assert len(ev.text_files) == 2
    contents = {f.relpath: f.content for f in ev.text_files}
    assert contents["a.md"] == "Hello world from a.md"
    assert contents["b.txt"] == "body of b"


def test_gather_truncates_oversize_text(monkeypatch, tmp_path):
    big = "X" * 80_000
    (tmp_path / "big.md").write_text(big)
    def fake_evidence(**_):
        return {
            "node_path": str(tmp_path),
            "child_map": [], "text_files": [
                {"path": str(tmp_path / "big.md"), "size": 80_000, "mtime_ms": 1, "ext": "md"},
            ], "pdf_files": [], "skipped_sample": [],
        }
    monkeypatch.setattr(sampler.wiki_io, "fetch_node_evidence", fake_evidence)
    cfg = Config(max_bytes_per_file=10_000)
    ev = sampler.gather(str(tmp_path), cfg)
    assert len(ev.text_files) == 1
    assert ev.text_files[0].content.endswith(" ... [truncated]")
    body = ev.text_files[0].content[:-len(" ... [truncated]")]
    assert len(body) == 10_000


def test_gather_skips_non_utf8(monkeypatch, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_bytes(b"\xff\xfe\xfa")
    def fake_evidence(**_):
        return {
            "node_path": str(tmp_path), "child_map": [],
            "text_files": [{"path": str(bad), "size": 3, "mtime_ms": 1, "ext": "csv"}],
            "pdf_files": [], "skipped_sample": [],
        }
    monkeypatch.setattr(sampler.wiki_io, "fetch_node_evidence", fake_evidence)
    ev = sampler.gather(str(tmp_path), Config())
    assert ev.text_files == [], "non-UTF8 file must be silently skipped"


def test_gather_handles_broken_pdf(monkeypatch, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not actually a PDF")
    def fake_evidence(**_):
        return {
            "node_path": str(tmp_path), "child_map": [], "text_files": [],
            "pdf_files": [{"path": str(bad), "size": 18, "mtime_ms": 1, "ext": "pdf"}],
            "skipped_sample": [],
        }
    monkeypatch.setattr(sampler.wiki_io, "fetch_node_evidence", fake_evidence)
    ev = sampler.gather(str(tmp_path), Config())
    assert ev.pdf_excerpts == [], "broken PDF must be skipped, not raise"


def test_gather_returns_skipped(monkeypatch, tmp_path):
    def fake_evidence(**_):
        return {
            "node_path": str(tmp_path), "child_map": [], "text_files": [],
            "pdf_files": [],
            "skipped_sample": [
                {"path": "/x/IMG.jpeg", "size": 1000000, "ext": "jpeg", "reason": "image"},
            ],
        }
    monkeypatch.setattr(sampler.wiki_io, "fetch_node_evidence", fake_evidence)
    ev = sampler.gather(str(tmp_path), Config())
    assert len(ev.skipped) == 1
    assert ev.skipped[0]["reason"] == "image"


def test_gather_propagates_http_error(monkeypatch, tmp_path):
    import httpx
    def fake_evidence(**_):
        raise httpx.HTTPError("wiki down")
    monkeypatch.setattr(sampler.wiki_io, "fetch_node_evidence", fake_evidence)
    with pytest.raises(httpx.HTTPError):
        sampler.gather(str(tmp_path), Config())


def test_evidence_is_empty_when_everything_empty():
    ev = sampler.Evidence(node_path="/x")
    assert ev.is_empty() is True


def test_evidence_not_empty_when_child_map_has_items():
    ev = sampler.Evidence(node_path="/x",
                          child_map=[{"name": "a", "size": 0, "is_dir": False, "ext": ""}])
    assert ev.is_empty() is False


def test_evidence_not_empty_when_skipped_has_items():
    ev = sampler.Evidence(node_path="/x",
                          skipped=[{"path": "/x/a.jpg", "ext": "jpg", "size": 1, "reason": "image"}])
    assert ev.is_empty() is False
