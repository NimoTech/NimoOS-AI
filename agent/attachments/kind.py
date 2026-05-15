import mimetypes
import os

try:
    import magic
    _HAS_MAGIC = True
except ImportError:
    _HAS_MAGIC = False

TEXT_EXT_WHITELIST = {
    "txt", "md", "csv", "json", "log",
    "py", "js", "ts", "go", "rs", "java", "c", "h", "cpp",
    "yaml", "yml", "ini", "toml", "conf",
    "html", "css", "xml", "sql", "sh",
}

_UTF8_PROBE = 8192  # bytes


def _detect_mime(path: str, fallback_name: str) -> str:
    if _HAS_MAGIC:
        try:
            return magic.from_file(path, mime=True) or "application/octet-stream"
        except Exception:
            pass
    guess, _ = mimetypes.guess_type(fallback_name)
    return guess or "application/octet-stream"


def _is_utf8_text(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(_UTF8_PROBE)
        if b"\x00" in chunk:
            return False
        chunk.decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def classify(path: str, original_name: str) -> tuple[str, str]:
    """
    Returns (mime, kind). kind in {image, text, video, audio, binary}.
    Order: magic-bytes major type for image/video/audio first; then text
    by (whitelist suffix AND utf-8 decodable); else binary.
    """
    mime = _detect_mime(path, original_name)
    major = mime.split("/", 1)[0] if "/" in mime else ""

    if major == "image":
        return mime, "image"
    if major == "video":
        return mime, "video"
    if major == "audio":
        return mime, "audio"

    ext = os.path.splitext(original_name)[1].lstrip(".").lower()
    if ext in TEXT_EXT_WHITELIST and _is_utf8_text(path):
        return mime if major == "text" else "text/plain", "text"

    if major == "text" and _is_utf8_text(path):
        return mime, "text"

    return mime, "binary"
