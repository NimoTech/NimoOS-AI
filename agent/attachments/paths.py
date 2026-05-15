import os
import re

_MAX_TOTAL = 200
_MAX_EXT = 16  # ".something"; longer means it's not really an extension


def sanitize_filename(name: str | None) -> str:
    if not name:
        return "untitled"
    # Take final component (strip any path)
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Strip nulls and ASCII control chars
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    if not name:
        return "untitled"

    base, ext = os.path.splitext(name)
    if len(ext) > _MAX_EXT or " " in ext:
        # Bogus extension; treat the whole thing as base
        base, ext = name, ""

    if len(base) + len(ext) <= _MAX_TOTAL:
        return base + ext
    # Reserve room for the extension
    base = base[: _MAX_TOTAL - len(ext)]
    return base + ext


def build_storage_path(data_root: str, session_id: str, attachment_id: str,
                       sanitized_filename: str) -> str:
    return os.path.join(data_root, "sessions", session_id, "attachments",
                        f"{attachment_id}__{sanitized_filename}")
