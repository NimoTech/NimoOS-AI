"""Worker main loop.

run_once() pulls a batch from /needs-summary, samples + LLM each, posts
back to /summary. Failure classification:
  - SamplerError / LLMError / JSONParseError → per-node, POST a "failed"
    placeholder so based_on_last_modified advances and the node leaves the
    queue until content changes.
  - Anything else (notably httpx.HTTPError from wiki_io) → transient, BREAK
    the round, do NOT pollute wiki_summaries. Next timer fire retries.

This split is what makes the design's "poison node defense" actually work.
"""
from __future__ import annotations
import argparse
import logging
import sys

from wiki_summary_worker import __version__, config, llm, sampler, wiki_io
from wiki_summary_worker.rate_limit import RateLimiter, RateLimitExceeded


log = logging.getLogger(__name__)


def _generator_version(cfg) -> str:
    return f"wiki-summary-worker/{__version__}+{cfg.model}"


def _generator_version_failed(cfg) -> str:
    return f"wiki-summary-worker/{__version__}+failed"


def _post_failed(node, cfg, reason: str) -> None:
    wiki_io.post_summary(
        path=node["path"],
        ai_label="(生成失败,等待变化后重试)",
        summary=f"_当前内容下摘要生成失败 ({reason})。目录有新变化后会自动重试。_",
        based_on_last_modified_ms=node["last_modified_ms"],
        generator_version=_generator_version_failed(cfg),
    )


def run_once(cfg) -> int:
    rate = RateLimiter()
    nodes = wiki_io.fetch_needs_summary(limit=cfg.batch_size)
    if not nodes:
        return 0

    processed = 0
    for node in nodes:
        try:
            rate.take_or_die(cfg.max_per_hour)
        except RateLimitExceeded as e:
            log.warning("rate limit hit, ending round: %s", e)
            break

        try:
            if node["child_count"] == 0:
                wiki_io.post_summary(
                    path=node["path"],
                    ai_label="空目录",
                    summary="此目录目前为空。",
                    based_on_last_modified_ms=node["last_modified_ms"],
                    generator_version=_generator_version(cfg),
                )
                processed += 1
                continue

            evidence = sampler.gather(node["path"], cfg)
            result = llm.summarize(evidence, cfg)
            wiki_io.post_summary(
                path=node["path"],
                ai_label=result["ai_label"],
                summary=result["summary"],
                based_on_last_modified_ms=node["last_modified_ms"],
                generator_version=_generator_version(cfg),
            )
            processed += 1

        except (sampler.SamplerError, llm.LLMError, llm.JSONParseError) as e:
            log.warning("per-node failure for %s (writing placeholder): %s",
                        node["path"], e)
            try:
                _post_failed(node, cfg, type(e).__name__)
                processed += 1
            except Exception as inner:
                log.error("post_failed also failed for %s: %s",
                          node["path"], inner)
        except Exception as e:
            log.warning("transient failure for %s, aborting round: %s",
                        node["path"], e)
            break

    return processed


def main() -> int:
    ap = argparse.ArgumentParser(description="NimoOS wiki summary worker")
    ap.add_argument("--path", help="Only summarize this one wiki_node path")
    ap.add_argument("--all", action="store_true", help="Keep running until queue empty")
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip LLM call; print evidence + prompt instead")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    if not cfg.enabled:
        log.info("[wiki-summary] disabled in config, exiting")
        return 0

    if args.path:
        return _run_one_path(args.path, cfg, dry_run=args.dry_run)

    if args.all:
        total = 0
        while True:
            n = run_once(cfg)
            total += n
            if n == 0:
                break
        log.info("all-mode finished, total processed=%d", total)
        return 0

    n = run_once(cfg)
    log.info("round finished, processed=%d", n)
    return 0


def _run_one_path(path: str, cfg, dry_run: bool) -> int:
    """Synchronous handler for `--path X`. CLI debug tool."""
    node = {
        "path": path, "level": "project",
        "last_modified_ms": 0, "current_ai_label": "", "child_count": 1,
    }
    try:
        evidence = sampler.gather(path, cfg)
    except Exception as e:
        log.error("--path: sampler failed: %s", e)
        return 1

    if dry_run:
        from wiki_summary_worker.prompt import SYSTEM, serialize_user_message
        ev_dict = evidence.to_dict()
        print(f"[sampler] node={path}")
        print(f"[sampler] child_map ({len(ev_dict['child_map'])} items)")
        print(f"[sampler] text_files ({len(ev_dict['text_files'])}):")
        for f in ev_dict["text_files"]:
            print(f"  - {f['relpath']} ({f['bytes']} bytes -> {len(f['content'])} chars)")
        print(f"[sampler] pdf_excerpts ({len(ev_dict['pdf_excerpts'])}):")
        for f in ev_dict["pdf_excerpts"]:
            print(f"  - {f['relpath']} ({f['bytes']} bytes -> {len(f['content'])} chars)")
        print(f"[sampler] skipped ({len(ev_dict['skipped'])}):")
        for s in ev_dict["skipped"][:5]:
            print(f"  - {s.get('path')} ({s.get('reason')})")
        print(f"[llm] system prompt ({len(SYSTEM)} chars)")
        print(f"[llm] user message ({len(serialize_user_message(evidence))} chars)")
        print(f"[llm] model: {cfg.model}")
        print("(dry-run: not actually calling LLM)")
        return 0

    try:
        result = llm.summarize(evidence, cfg)
        wiki_io.post_summary(
            path=path, **result,
            based_on_last_modified_ms=node["last_modified_ms"],
            generator_version=_generator_version(cfg),
        )
        print(f"[ok] {path} -> {result['ai_label']}")
    except Exception as e:
        log.error("--path: summarize failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
