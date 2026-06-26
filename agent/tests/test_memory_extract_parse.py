import memory_extract as mx


def test_build_prompt_includes_existing_and_history():
    p = mx.build_extraction_prompt(
        history=[{"role": "user", "content": "I'm a software engineer"}],
        existing=[{"id": "m1", "kind": "fact", "text": "lives in Berlin"}])
    assert "m1" in p and "lives in Berlin" in p
    assert "software engineer" in p
    assert "ADD" in p and "UPDATE" in p and "NOOP" in p   # action vocabulary stated
    assert "JSON" in p or "json" in p


def test_parse_valid_with_fences():
    text = '```json\n{"actions":[{"op":"ADD","kind":"fact","text":"is an engineer"}],"referenced":["m1"]}\n```'
    out = mx.parse_extraction(text)
    assert out == {"actions": [{"op": "ADD", "kind": "fact", "text": "is an engineer",
                                "priority": 0, "id": None}],
                   "referenced": ["m1"]}


def test_parse_drops_malformed_actions():
    text = ('{"actions":[{"op":"ADD","text":"no kind"},'          # ADD missing kind -> drop
            '{"op":"UPDATE","id":"m2","kind":"goal","text":"x"},'  # ok
            '{"op":"BOGUS","id":"m3"},'                            # bad op -> drop
            '{"op":"NOOP","id":"m4"}],"referenced":["m5",7]}')     # 7 not a str -> dropped
    out = mx.parse_extraction(text)
    ops = [(a["op"], a.get("id"), a.get("kind")) for a in out["actions"]]
    assert ops == [("UPDATE", "m2", "goal"), ("NOOP", "m4", None)]
    assert out["referenced"] == ["m5"]


def test_parse_returns_none_on_garbage():
    assert mx.parse_extraction("not json at all") is None
    assert mx.parse_extraction("[1,2,3]") is None        # not an object
    assert mx.parse_extraction("") is None
