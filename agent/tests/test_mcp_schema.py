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
    assert flatten_result(Res()) == "hello\nworld"


def test_flatten_error_marks_error():
    class Res:
        content = []
        isError = True
    out = flatten_result(Res())
    assert "error" in out.lower()
