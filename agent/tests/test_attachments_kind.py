import io
from attachments.kind import classify, TEXT_EXT_WHITELIST


def _write(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_png_classified_as_image(tmp_path):
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    path = _write(tmp_path, "x.png", data)
    mime, kind = classify(path, "x.png")
    assert kind == "image"
    assert mime.startswith("image/")


def test_utf8_text_with_whitelist_ext(tmp_path):
    path = _write(tmp_path, "notes.md", "# 标题\n".encode("utf-8"))
    mime, kind = classify(path, "notes.md")
    assert kind == "text"


def test_utf8_no_whitelist_ext_is_binary(tmp_path):
    path = _write(tmp_path, "weird.qqq", "hello".encode("utf-8"))
    mime, kind = classify(path, "weird.qqq")
    assert kind == "binary"


def test_binary_file(tmp_path):
    path = _write(tmp_path, "blob.bin", bytes(range(256)) * 4)
    mime, kind = classify(path, "blob.bin")
    assert kind == "binary"


def test_text_whitelist_covers_common_code():
    for ext in ["py", "js", "ts", "go", "yaml", "json", "log", "csv", "txt", "md"]:
        assert ext in TEXT_EXT_WHITELIST


def test_invalid_utf8_text_extension_is_binary(tmp_path):
    path = _write(tmp_path, "bad.txt", b"\xff\xfe\x00binary\xff")
    mime, kind = classify(path, "bad.txt")
    assert kind == "binary"
