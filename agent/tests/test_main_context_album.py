import importlib
import sys

import pytest


@pytest.fixture
def main_mod(tmp_path, monkeypatch):
    # Same isolation pattern as test_main_agent_type.py: fresh DB + clean
    # module graph so main's import-time DB init hits the temp path.
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    for mod in ["main", "agent", "db"]:
        sys.modules.pop(mod, None)
    import main
    return importlib.reload(main)


def test_run_request_accepts_context_album(main_mod):
    req = main_mod.RunRequest(
        message="hi", context_album={"id": "alb-1", "name": "Tokyo"})
    assert req.context_album.id == "alb-1"
    assert req.context_album.name == "Tokyo"


def test_run_request_context_album_defaults_none(main_mod):
    req = main_mod.RunRequest(message="hi")
    assert req.context_album is None


def test_context_album_name_optional(main_mod):
    req = main_mod.RunRequest(message="hi", context_album={"id": "alb-1"})
    assert req.context_album.name == ""
