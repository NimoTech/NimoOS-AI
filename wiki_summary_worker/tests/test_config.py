from __future__ import annotations
import textwrap
from wiki_summary_worker.config import Config, load


def test_load_defaults_when_section_missing(tmp_path):
    p = tmp_path / "wiki.conf"
    p.write_text(textwrap.dedent("""\
        [wiki]
        DefaultScanIntervalSec = 21600
    """))
    cfg = load(str(p))
    assert cfg.enabled is True
    assert cfg.batch_size == 3
    assert cfg.max_per_hour == 100
    assert cfg.model == "gpt-4o-mini"
    assert cfg.max_files_per_node == 20
    assert cfg.max_bytes_per_file == 51200


def test_load_overrides_from_file(tmp_path):
    p = tmp_path / "wiki.conf"
    p.write_text(textwrap.dedent("""\
        [wiki-summary]
        Enabled = false
        BatchSize = 5
        MaxPerHour = 25
        Model = claude-sonnet-4-6
        MaxFilesPerNode = 50
        MaxBytesPerFile = 102400
    """))
    cfg = load(str(p))
    assert cfg.enabled is False
    assert cfg.batch_size == 5
    assert cfg.max_per_hour == 25
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.max_files_per_node == 50
    assert cfg.max_bytes_per_file == 102400


def test_load_returns_defaults_when_file_missing(tmp_path):
    cfg = load(str(tmp_path / "nonexistent.conf"))
    assert cfg.enabled is True
    assert cfg.batch_size == 3
