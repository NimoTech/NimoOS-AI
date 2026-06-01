from __future__ import annotations
import httpx
from unittest.mock import MagicMock
from wiki_summary_worker import worker, llm, sampler
from wiki_summary_worker.config import Config


def _node(path, child_count=5, last_modified_ms=1000):
    return {
        "path": path, "level": "project",
        "last_modified_ms": last_modified_ms,
        "current_ai_label": "", "child_count": child_count,
    }


def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(worker, "RateLimiter",
                        MagicMock(return_value=MagicMock(take_or_die=lambda _: None)))


def test_run_once_short_circuits_on_empty_evidence(monkeypatch):
    monkeypatch.setattr(worker.wiki_io, "fetch_needs_summary",
                        lambda limit: [_node("/empty", child_count=99)])  # child_count is now irrelevant
    posted = []
    monkeypatch.setattr(worker.wiki_io, "post_summary",
                        lambda **kw: posted.append(kw))
    monkeypatch.setattr(worker.sampler, "gather",
                        lambda path, cfg: sampler.Evidence(node_path=path))  # all fields default to empty
    _no_rate_limit(monkeypatch)

    cfg = Config()
    n = worker.run_once(cfg)
    assert n == 1
    assert len(posted) == 1
    assert posted[0]["ai_label"] == "空目录"
    assert "+failed" not in posted[0]["generator_version"]


def test_run_once_with_nonzero_child_count_but_empty_evidence(monkeypatch):
    monkeypatch.setattr(worker.wiki_io, "fetch_needs_summary",
                        lambda limit: [_node("/empty", child_count=5)])
    posted = []
    monkeypatch.setattr(worker.wiki_io, "post_summary",
                        lambda **kw: posted.append(kw))
    monkeypatch.setattr(worker.sampler, "gather",
                        lambda path, cfg: sampler.Evidence(node_path=path))  # all fields default to empty
    _no_rate_limit(monkeypatch)

    cfg = Config()
    n = worker.run_once(cfg)
    assert n == 1
    assert len(posted) == 1
    assert posted[0]["ai_label"] == "空目录"
    assert "+failed" not in posted[0]["generator_version"]


def test_run_once_writes_placeholder_on_llm_error(monkeypatch):
    monkeypatch.setattr(worker.wiki_io, "fetch_needs_summary",
                        lambda limit: [_node("/x")])
    posted = []
    monkeypatch.setattr(worker.wiki_io, "post_summary",
                        lambda **kw: posted.append(kw))
    monkeypatch.setattr(worker.sampler, "gather",
                        lambda path, cfg: sampler.Evidence(node_path=path,
                            text_files=[sampler.FileExcerpt("a.md", 5, "hello")]))
    monkeypatch.setattr(worker.llm, "summarize",
                        lambda ev, cfg: (_ for _ in ()).throw(llm.LLMError("boom")))
    _no_rate_limit(monkeypatch)

    assert worker.run_once(Config()) == 1
    assert len(posted) == 1
    assert "+failed" in posted[0]["generator_version"]
    assert "生成失败" in posted[0]["ai_label"]


def test_run_once_writes_placeholder_on_jsonparse(monkeypatch):
    monkeypatch.setattr(worker.wiki_io, "fetch_needs_summary",
                        lambda limit: [_node("/x")])
    posted = []
    monkeypatch.setattr(worker.wiki_io, "post_summary",
                        lambda **kw: posted.append(kw))
    monkeypatch.setattr(worker.sampler, "gather",
                        lambda path, cfg: sampler.Evidence(node_path=path,
                            text_files=[sampler.FileExcerpt("a.md", 5, "hello")]))
    monkeypatch.setattr(worker.llm, "summarize",
                        lambda ev, cfg: (_ for _ in ()).throw(llm.JSONParseError("bad")))
    _no_rate_limit(monkeypatch)

    assert worker.run_once(Config()) == 1
    assert len(posted) == 1
    assert "+failed" in posted[0]["generator_version"]


def test_run_once_writes_placeholder_on_sampler_error(monkeypatch):
    monkeypatch.setattr(worker.wiki_io, "fetch_needs_summary",
                        lambda limit: [_node("/x")])
    posted = []
    monkeypatch.setattr(worker.wiki_io, "post_summary",
                        lambda **kw: posted.append(kw))
    monkeypatch.setattr(worker.sampler, "gather",
                        lambda path, cfg: (_ for _ in ()).throw(sampler.SamplerError("perm denied")))
    _no_rate_limit(monkeypatch)

    assert worker.run_once(Config()) == 1
    assert "+failed" in posted[0]["generator_version"]


def test_run_once_breaks_on_transient_failure(monkeypatch):
    monkeypatch.setattr(worker.wiki_io, "fetch_needs_summary",
                        lambda limit: [_node("/a"), _node("/b")])
    posted = []
    monkeypatch.setattr(worker.wiki_io, "post_summary",
                        lambda **kw: posted.append(kw))
    def gather_side_effect(path, cfg):
        if path == "/a":
            raise httpx.HTTPError("wiki down")
        return sampler.Evidence(node_path=path)
    monkeypatch.setattr(worker.sampler, "gather", gather_side_effect)
    _no_rate_limit(monkeypatch)

    assert worker.run_once(Config()) == 0, "transient failure must break round, no posts"
    assert posted == []


def test_run_once_writes_real_summary_on_success(monkeypatch):
    monkeypatch.setattr(worker.wiki_io, "fetch_needs_summary",
                        lambda limit: [_node("/x")])
    posted = []
    monkeypatch.setattr(worker.wiki_io, "post_summary",
                        lambda **kw: posted.append(kw))
    monkeypatch.setattr(worker.sampler, "gather",
                        lambda path, cfg: sampler.Evidence(node_path=path,
                            text_files=[sampler.FileExcerpt("a.md", 5, "hello")]))
    monkeypatch.setattr(worker.llm, "summarize",
                        lambda ev, cfg: {"ai_label": "L", "summary": "S"})
    _no_rate_limit(monkeypatch)

    assert worker.run_once(Config()) == 1
    assert posted[0]["ai_label"] == "L"
    assert posted[0]["summary"] == "S"
    assert "+failed" not in posted[0]["generator_version"]


def test_run_once_breaks_on_rate_limit(monkeypatch):
    from wiki_summary_worker.rate_limit import RateLimitExceeded
    monkeypatch.setattr(worker.wiki_io, "fetch_needs_summary",
                        lambda limit: [_node("/a"), _node("/b")])
    posted = []
    monkeypatch.setattr(worker.wiki_io, "post_summary",
                        lambda **kw: posted.append(kw))
    def take_or_die(_):
        raise RateLimitExceeded("hit cap")
    monkeypatch.setattr(worker, "RateLimiter",
                        MagicMock(return_value=MagicMock(take_or_die=take_or_die)))

    assert worker.run_once(Config()) == 0
    assert posted == []
