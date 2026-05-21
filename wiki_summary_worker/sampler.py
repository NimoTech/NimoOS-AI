"""File sampler — turns a wiki_node path into Evidence for the LLM.

Contracts:
  - httpx.HTTPError from wiki_io.fetch_node_evidence is NOT wrapped here.
    That means "wiki unreachable" — a transient round-level failure.
  - Per-file local errors (UnicodeDecodeError, OSError, malformed PDF)
    are logged at debug and that file is skipped. Partial evidence is OK.
  - SamplerError exists for future use (e.g. a node-level fault we can't
    recover from); none of the current paths raise it, but worker.run_once
    catches it as a per-node failure class.
"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from wiki_summary_worker import wiki_io
from wiki_summary_worker.config import Config


log = logging.getLogger(__name__)


class SamplerError(Exception):
    """Raised by gather() ONLY when the node cannot be sampled at all.
    Per-file errors are logged + skipped, not raised."""


@dataclass
class FileExcerpt:
    relpath: str
    bytes: int
    content: str


@dataclass
class Evidence:
    node_path: str
    child_map: list[dict[str, Any]] = field(default_factory=list)
    text_files: list[FileExcerpt] = field(default_factory=list)
    pdf_excerpts: list[FileExcerpt] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_path": self.node_path,
            "child_map": self.child_map,
            "text_files": [
                {"relpath": e.relpath, "bytes": e.bytes, "content": e.content}
                for e in self.text_files
            ],
            "pdf_excerpts": [
                {"relpath": e.relpath, "bytes": e.bytes, "content": e.content}
                for e in self.pdf_excerpts
            ],
            "skipped": self.skipped,
        }


_TRUNC = " ... [truncated]"


def gather(node_path: str, cfg: Config) -> Evidence:
    """Build Evidence for one wiki_node. Calls Wiki's /node-evidence to pick
    files (via file_index), reads their content from disk."""
    # HTTPError from wiki_io intentionally leaks — see module docstring.
    api = wiki_io.fetch_node_evidence(
        path=node_path,
        text_limit=cfg.max_text_files,
        pdf_limit=cfg.max_pdf_files,
    )

    text_files: list[FileExcerpt] = []
    for f in api.get("text_files", []):
        try:
            with open(f["path"], "r", encoding="utf-8", errors="strict") as fh:
                raw = fh.read(cfg.max_bytes_per_file + 4096)
        except (UnicodeDecodeError, OSError) as e:
            log.debug("sampler: skip text %s: %s", f["path"], e)
            continue
        truncated = ""
        if len(raw) > cfg.max_bytes_per_file:
            raw = raw[: cfg.max_bytes_per_file]
            truncated = _TRUNC
        text_files.append(FileExcerpt(
            relpath=os.path.relpath(f["path"], node_path),
            bytes=f.get("size", 0),
            content=raw + truncated,
        ))

    pdf_excerpts: list[FileExcerpt] = []
    for f in api.get("pdf_files", []):
        try:
            from pypdf import PdfReader  # local import — heavy
            reader = PdfReader(f["path"])
            text = ""
            for page in reader.pages[:3]:
                text += page.extract_text() or ""
                if len(text) >= 15360:
                    break
            pdf_excerpts.append(FileExcerpt(
                relpath=os.path.relpath(f["path"], node_path),
                bytes=f.get("size", 0),
                content=text[:15360],
            ))
        except Exception as e:
            log.debug("sampler: pdf extract failed %s: %s", f["path"], e)

    return _enforce_total_cap(Evidence(
        node_path=node_path,
        child_map=api.get("child_map", []),
        text_files=text_files,
        pdf_excerpts=pdf_excerpts,
        skipped=api.get("skipped_sample", []),
    ), cap=204_800)


def _enforce_total_cap(ev: Evidence, *, cap: int) -> Evidence:
    def char_count() -> int:
        n = sum(len(e.content) for e in ev.text_files)
        n += sum(len(e.content) for e in ev.pdf_excerpts)
        return n

    while char_count() > cap and ev.pdf_excerpts:
        ev.pdf_excerpts.pop()
    while char_count() > cap and ev.text_files:
        ev.text_files.pop()
    return ev
