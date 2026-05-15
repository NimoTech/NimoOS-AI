import pytest
from attachments.paths import sanitize_filename, build_storage_path


def test_strips_path_separators():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a/b\\c.txt") == "c.txt"


def test_strips_nulls_and_controls():
    assert sanitize_filename("foo\x00bar.txt") == "foobar.txt"


def test_empty_name_fallback():
    assert sanitize_filename("") == "untitled"
    assert sanitize_filename(None) == "untitled"
    assert sanitize_filename("/") == "untitled"


def test_long_name_preserves_extension():
    name = ("a" * 300) + ".mp4"
    out = sanitize_filename(name)
    assert out.endswith(".mp4"), out
    assert len(out) <= 200, len(out)


def test_long_name_long_extension_dropped():
    name = ("a" * 300) + ".thisisaverylongextensionname"
    out = sanitize_filename(name)
    assert len(out) <= 200
    assert out == "a" * 200


def test_multiple_dots_keeps_only_last():
    assert sanitize_filename("archive.tar.gz") == "archive.tar.gz"
    long = ("x" * 300) + ".tar.gz"
    out = sanitize_filename(long)
    assert out.endswith(".gz")


def test_build_storage_path():
    p = build_storage_path("/data/agent", "s1", "a1", "photo.png")
    assert p == "/data/agent/sessions/s1/attachments/a1__photo.png"
