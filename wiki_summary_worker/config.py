"""Configuration loader.

Reads `[wiki-summary]` section from /etc/nimoos/wiki.conf (or the path passed
to load()). Falls back to sane defaults when the section or any individual
key is absent.
"""
from __future__ import annotations
import configparser
from dataclasses import dataclass


@dataclass
class Config:
    enabled: bool = True
    batch_size: int = 3
    max_per_hour: int = 100
    model: str = "gpt-4o-mini"
    max_files_per_node: int = 20
    max_bytes_per_file: int = 51200
    max_text_files: int = 10
    max_pdf_files: int = 5
    user_id_header: str = "system"  # passed as X-NimoOS-User-ID to chat-completions

    @property
    def llm_timeout_sec(self) -> int:
        return 60


def load(path: str = "/etc/nimoos/wiki.conf") -> Config:
    """Load worker config from the [wiki-summary] section of an INI file.
    Missing file or missing section both yield Config()'s defaults."""
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except (configparser.Error, OSError):
        return Config()
    if not parser.has_section("wiki-summary"):
        return Config()
    sec = parser["wiki-summary"]
    return Config(
        enabled=sec.getboolean("Enabled", True),
        batch_size=sec.getint("BatchSize", 3),
        max_per_hour=sec.getint("MaxPerHour", 100),
        model=sec.get("Model", "gpt-4o-mini"),
        max_files_per_node=sec.getint("MaxFilesPerNode", 20),
        max_bytes_per_file=sec.getint("MaxBytesPerFile", 51200),
        max_text_files=sec.getint("MaxTextFiles", 10),
        max_pdf_files=sec.getint("MaxPdfFiles", 5),
        user_id_header=sec.get("UserIdHeader", "system"),
    )
