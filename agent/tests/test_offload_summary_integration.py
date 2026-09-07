"""Reading guide survives L1 micro-compaction and is returned by fs.read_file."""
import asyncio
import types

import pytest

import compaction_filter as cf
import offload_summary as osum
import tool_output as to
from fs import ops as fs_ops


def _guided_placeholder(path, chars=8000):
    ph = to.make_placeholder("x" * chars, tool_name="web_fetch", path=path, chars=chars)
    block = osum.render_block("Key facts:\n- a\nSections:\nL1-9  body", total_lines=9)
    m = to.TRAILER_RE.search(ph)
    return block + "\n" + ph[m.start():]


def test_l1_keeps_whole_reading_guide_and_is_idempotent(tmp_path):
    path = str(tmp_path / "c1.txt")
    out = _guided_placeholder(path)
    m = {"type": "function_call_output", "call_id": "c1", "output": out}
    new = cf._compact_output(m, "web_fetch", 800)
    assert new is not None
    text = new["output"]
    assert text.startswith('<untrusted-data source="tool-output-summary">')
    assert "L1-9  body" in text
    assert text.rstrip().endswith(f"[tool output offloaded: chars=8000 path={path}]")
    assert "Read it in small slices" not in text          # advice dropped
    assert cf._compact_output({**m, "output": text}, "web_fetch", 800) is None   # second pass: no-op


def test_l1_without_guide_still_cuts_head(tmp_path):
    path = str(tmp_path / "c2.txt")
    ph = to.make_placeholder("x" * 8000, tool_name="t", path=path, chars=8000)
    new = cf._compact_output({"type": "function_call_output", "call_id": "c2", "output": ph}, "t", 800)
    assert new is not None and "\n…\n[tool output offloaded" in new["output"]


@pytest.mark.asyncio
async def test_read_file_on_offload_returns_guide_when_sidecar_exists(tmp_path, monkeypatch):
    raw = tmp_path / "c3.txt"
    raw.write_text("line\n" * 3000)                      # > OFFLOAD_THRESHOLD_CHARS
    (tmp_path / "c3.summary.txt").write_text("Key facts:\n- three thousand lines")
    monkeypatch.setattr(to, "is_offload_path", lambda p: True)

    async def _gate(ctx, path, mode):
        return str(raw)
    monkeypatch.setattr(fs_ops, "_resolve_and_gate_or_request", _gate)
    out = await fs_ops.read_file(types.SimpleNamespace(), str(raw))
    assert out.startswith('<untrusted-data source="tool-output-summary">')
    assert "three thousand lines" in out
    assert "3000 lines in the file" in out
    assert "read_file_lines(path, start, end) using the ranges above" in out


@pytest.mark.asyncio
async def test_read_file_on_offload_without_sidecar_keeps_head_preview(tmp_path, monkeypatch):
    raw = tmp_path / "c4.txt"
    raw.write_text("line\n" * 3000)
    monkeypatch.setattr(to, "is_offload_path", lambda p: True)

    async def _gate(ctx, path, mode):
        return str(raw)
    monkeypatch.setattr(fs_ops, "_resolve_and_gate_or_request", _gate)
    out = await fs_ops.read_file(types.SimpleNamespace(), str(raw))
    assert out.startswith('<untrusted-data source="tool-output">')
    assert "already a saved tool result" in out
