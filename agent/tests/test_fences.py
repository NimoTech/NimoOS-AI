from fences import fence_untrusted


def test_wraps_and_labels():
    out = fence_untrusted("wiki:/DATA/notes", "hello world")
    assert out.startswith('<untrusted-data source="wiki:/DATA/notes">')
    assert out.rstrip().endswith("</untrusted-data>")
    assert "hello world" in out


def test_strips_control_and_angle_brackets():
    out = fence_untrusted("s", "a\x00b<script>c\x07")
    assert "\x00" not in out and "\x07" not in out
    # angle brackets inside the payload are removed so nested fake tags can't
    # break out of / spoof the wrapper
    assert "<script>" not in out
    assert "abscriptc" in out or "abscript c" in out  # brackets gone, text kept


def test_newlines_preserved():
    out = fence_untrusted("s", "line1\nline2")
    assert "line1\nline2" in out


def test_source_label_sanitized():
    # a malicious source label cannot inject a closing tag / attributes
    out = fence_untrusted('s"><x>', "body")
    assert '<x>' not in out
    assert out.count("<untrusted-data") == 1


def test_cap_truncates():
    out = fence_untrusted("s", "x" * 10000, cap=100)
    # payload truncated to ~cap; wrapper still closed
    assert len(out) < 400
    assert out.rstrip().endswith("</untrusted-data>")


def test_empty_returns_empty():
    assert fence_untrusted("s", "   ") == ""
    assert fence_untrusted("s", "") == ""
