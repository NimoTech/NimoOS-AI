from mcp_client.hashing import schema_hash, desc_hash


def test_schema_hash_is_key_order_independent():
    a = {"type": "object", "properties": {"x": {"type": "string"}, "y": {"type": "int"}}}
    b = {"properties": {"y": {"type": "int"}, "x": {"type": "string"}}, "type": "object"}
    assert schema_hash(a) == schema_hash(b)


def test_schema_hash_changes_on_real_change():
    a = {"type": "object", "properties": {"x": {"type": "string"}}}
    b = {"type": "object", "properties": {"x": {"type": "number"}}}
    assert schema_hash(a) != schema_hash(b)


def test_schema_hash_none_equals_empty_object():
    # Server may not provide inputSchema; None and {} must converge,
    # otherwise each probe would determine "interface changed" and re-ask the user.
    assert schema_hash(None) == schema_hash({})


def test_hashes_are_16_hex_chars():
    h = schema_hash({"a": 1})
    assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)


def test_desc_hash_distinguishes_and_handles_none():
    assert desc_hash("send an email") != desc_hash("send an email and cc the admin")
    assert desc_hash(None) == desc_hash("")


def test_non_ascii_is_stable():
    # ensure_ascii=False is part of the algorithm: switching it would change
    # the hash of every description with non-ASCII characters
    assert desc_hash("发送邮件") == desc_hash("发送邮件")
