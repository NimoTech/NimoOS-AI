from types import SimpleNamespace

import agent as agent_module


def test_format_context_photo_only():
    photo = SimpleNamespace(name="a.jpg", takenAt="2026-01-01", place="Tokyo")
    out = agent_module.format_context_lines(context_photo=photo)
    assert '[Viewing photo: "a.jpg"' in out
    assert "taken 2026-01-01" in out
    assert "location: Tokyo" in out


def test_format_context_photo_omits_empty_fields():
    photo = SimpleNamespace(name="a.jpg", takenAt="", place="")
    out = agent_module.format_context_lines(context_photo=photo)
    assert "taken" not in out
    assert "location" not in out


def test_format_context_album_only():
    album = SimpleNamespace(id="alb-1", name="Tokyo · Spring")
    out = agent_module.format_context_lines(context_album=album)
    assert '[Target album: "Tokyo · Spring" (album_id: alb-1)' in out
    assert "do NOT create a new one" in out


def test_format_context_both():
    photo = SimpleNamespace(name="a.jpg", takenAt="", place="")
    album = SimpleNamespace(id="alb-1", name="T")
    out = agent_module.format_context_lines(photo, album)
    assert "Viewing photo" in out
    assert "Target album" in out


def test_format_context_none_is_empty():
    assert agent_module.format_context_lines() == ""
