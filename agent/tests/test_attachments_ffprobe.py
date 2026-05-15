import subprocess
import json
from unittest.mock import patch
from attachments.ffprobe import probe


_OK_OUTPUT = json.dumps({
    "format": {"duration": "12.5", "bit_rate": "5000000"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
})


def test_success_returns_simplified(tmp_path):
    fake = subprocess.CompletedProcess(args=[], returncode=0,
                                       stdout=_OK_OUTPUT.encode(), stderr=b"")
    with patch("subprocess.run", return_value=fake):
        result = probe("/whatever.mp4", timeout=5)
    assert result["ok"] is True
    assert result["duration"] == 12.5
    assert result["width"] == 1920
    assert result["height"] == 1080
    assert result["codec"] == "h264"
    assert result["bitrate"] == 5000000
    assert any(t["codec"] == "aac" for t in result["tracks"])


def test_not_found_returns_failure():
    with patch("subprocess.run", side_effect=FileNotFoundError("ffprobe")):
        result = probe("/whatever.mp4", timeout=5)
    assert result["ok"] is False
    assert result["error"] == "not_found"


def test_timeout_returns_failure():
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=5)):
        result = probe("/whatever.mp4", timeout=5)
    assert result["ok"] is False
    assert result["error"] == "timeout"


def test_non_zero_exit_returns_failure():
    fake = subprocess.CompletedProcess(args=[], returncode=1,
                                       stdout=b"", stderr=b"bad")
    with patch("subprocess.run", return_value=fake):
        result = probe("/whatever.mp4", timeout=5)
    assert result["ok"] is False
    assert result["error"].startswith("exit_")


def test_garbage_output_returns_failure():
    fake = subprocess.CompletedProcess(args=[], returncode=0,
                                       stdout=b"not json", stderr=b"")
    with patch("subprocess.run", return_value=fake):
        result = probe("/whatever.mp4", timeout=5)
    assert result["ok"] is False
    assert result["error"] == "parse_error"
