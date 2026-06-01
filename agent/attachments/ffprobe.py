import json
import subprocess


def probe(path: str, timeout: float = 5.0) -> dict:
    """
    Run ffprobe and return a simplified dict.
    On success:  {"ok": True, "duration":..., "width":..., "height":...,
                  "codec":..., "bitrate":..., "tracks": [{"type":..., "codec":...}]}
    On failure:  {"ok": False, "error": "not_found"|"timeout"|"exit_N"|"parse_error"}
    """
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}

    if cp.returncode != 0:
        return {"ok": False, "error": f"exit_{cp.returncode}"}

    try:
        data = json.loads(cp.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {"ok": False, "error": "parse_error"}

    streams = data.get("streams", []) or []
    fmt = data.get("format", {}) or {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    tracks = [
        {"type": s.get("codec_type"), "codec": s.get("codec_name")}
        for s in streams
    ]

    def _maybe_num(v, cast):
        try:
            return cast(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "ok": True,
        "duration": _maybe_num(fmt.get("duration"), float),
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "codec": video.get("codec_name") if video else None,
        "bitrate": _maybe_num(fmt.get("bit_rate"), int),
        "tracks": tracks,
    }
