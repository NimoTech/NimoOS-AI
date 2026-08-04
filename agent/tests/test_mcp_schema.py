import mcp.types as mcp_types

from mcp_client.schema import sanitize_schema, flatten_result


def test_sanitize_none_becomes_object():
    s = sanitize_schema(None)
    assert s["type"] == "object"
    assert s["properties"] == {}


def test_sanitize_passes_through_object():
    s = sanitize_schema({"type": "object",
                         "properties": {"q": {"type": "string"}},
                         "required": ["q"]})
    assert s["properties"]["q"]["type"] == "string"
    assert s["required"] == ["q"]


def test_flatten_text_content():
    class Block:
        def __init__(self, text): self.type = "text"; self.text = text
    class Res:
        content = [Block("hello"), Block("world")]
        isError = False
    out = flatten_result(Res())
    # untrusted MCP output is fenced as data, content preserved intact
    assert "hello\nworld" in out
    assert '<untrusted-data source="mcp-result">' in out
    assert out.rstrip().endswith("</untrusted-data>")


def test_flatten_error_marks_error():
    class Res:
        content = []
        isError = True
    out = flatten_result(Res())
    assert "error" in out.lower()


def test_flatten_real_call_tool_result_marks_error():
    """Regression pin: mcp 2.0's real CallToolResult exposes the error flag as the
    Python attribute `is_error` (snake_case) — `isError` is only its JSON wire
    alias, not a Python attribute. A duck-typed fake (like the one above, which
    sets `.isError` directly) can't catch a wrong attribute name because it
    happily provides whatever attribute the code asks for. This test must use the
    real SDK type so a regression to `getattr(result, "isError", False)` (which
    silently returns the False default and swallows every real tool error) fails
    loudly here instead of shipping."""
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="boom")],
        is_error=True,
    )
    assert not hasattr(result, "isError")  # confirms the wire alias is NOT a Python attr
    out = flatten_result(result)
    # body is fenced as untrusted data (see fence_untrusted below), so the marker
    # is inside the fence, not at the very start of the string.
    assert "[tool error] boom" in out


def test_flatten_real_call_tool_result_success_not_marked_error():
    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")],
        is_error=False,
    )
    out = flatten_result(result)
    assert "[tool error]" not in out
