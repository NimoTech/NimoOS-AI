import importlib
import os
import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "agent.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("NIMOOS_AGENT_DATA_ROOT", str(tmp_path))
    import db as db_module
    importlib.reload(db_module)
    conn = db_module.init_db(db_path, snapshots_root=str(tmp_path / "snaps"))
    # Make this conn the module-level _conn so get_connection() returns it
    db_module._conn = conn
    import agent as ag_module
    importlib.reload(ag_module)
    conn.execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
        ("s1", "u1"))
    conn.commit()
    return conn, tmp_path, ag_module


def _mk_att(conn, root, *, aid, kind, mime, filename, body=b"x", msg_id="m1"):
    rel = f"{aid}__{filename}"
    p = root / "sessions" / "s1" / "attachments"
    p.mkdir(parents=True, exist_ok=True)
    (p / rel).write_bytes(body)
    conn.execute(
        "INSERT INTO attachments "
        "(id,session_id,message_id,filename,mime,kind,size_bytes,rel_path,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, "s1", msg_id, filename, mime, kind, len(body), rel, 0))
    conn.commit()


def test_image_appears_inline_as_input_image(setup):
    conn, root, ag = setup
    _mk_att(conn, root, aid="i1", kind="image", mime="image/png",
            filename="x.png", body=b"\x89PNG\x00\x00")
    content = ag.build_user_content("hello", ["i1"], session_id="s1",
                                    data_root=str(root),
                                    model_name="gpt-4o",
                                    provider_type="openai")
    assert isinstance(content, list)
    assert content[0] == {"type": "input_text", "text": "hello"}
    img = next(c for c in content if c["type"] == "input_image")
    assert img["image_url"].startswith("data:image/png;base64,")


def test_non_image_does_not_appear_inline(setup):
    conn, root, ag = setup
    _mk_att(conn, root, aid="t1", kind="text", mime="text/plain",
            filename="x.txt", body=b"abc")
    content = ag.build_user_content("read this", ["t1"], session_id="s1",
                                    data_root=str(root))
    assert all(c["type"] != "input_image" for c in content)


def test_no_attachments_returns_string(setup):
    conn, root, ag = setup
    content = ag.build_user_content("hi", [], session_id="s1",
                                    data_root=str(root))
    assert content == "hi"


def test_select_tools_includes_read_attachment_when_non_image(setup):
    conn, root, ag = setup
    _mk_att(conn, root, aid="t2", kind="text", mime="text/plain",
            filename="x.txt")
    tools = ag.select_tools_for_run(["t2"], session_id="s1")
    # Find read_attachment by its tool name attribute
    names = []
    for t in tools:
        n = getattr(t, "name", None) or getattr(t, "tool_name", None) \
            or getattr(t, "__name__", None)
        names.append(n)
    assert "read_attachment" in names


def test_select_tools_excludes_read_attachment_when_all_image(setup):
    conn, root, ag = setup
    _mk_att(conn, root, aid="i2", kind="image", mime="image/png",
            filename="x.png")
    tools = ag.select_tools_for_run(["i2"], session_id="s1")
    names = []
    for t in tools:
        n = getattr(t, "name", None) or getattr(t, "tool_name", None) \
            or getattr(t, "__name__", None)
        names.append(n)
    assert "read_attachment" not in names


def test_select_tools_no_attachments(setup):
    conn, root, ag = setup
    tools = ag.select_tools_for_run([], session_id="s1")
    names = []
    for t in tools:
        n = getattr(t, "name", None) or getattr(t, "tool_name", None) \
            or getattr(t, "__name__", None)
        names.append(n)
    assert "read_attachment" not in names


def test_hydrate_image_blocks_inlines_base64_from_attachment_id(setup):
    # On a follow-up turn the run loop reads stored history that contains
    # the compact {type:input_image, attachment_id:...} shape — without
    # rehydration the SDK adapter raises "Only image URLs are supported
    # for input_image". hydrate_image_blocks must turn it back into a
    # real image_url data URL.
    conn, root, ag = setup
    body = b"\x89PNG\x00\x01\x02\x03"
    _mk_att(conn, root, aid="i9", kind="image", mime="image/png",
            filename="x.png", body=body)
    history = [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "earlier turn"},
            {"type": "input_image", "attachment_id": "i9"},
        ],
    }]
    out = ag.hydrate_image_blocks(history, session_id="s1",
                                  data_root=str(root))
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "input_text", "text": "earlier turn"}
    assert blocks[1]["type"] == "input_image"
    assert blocks[1]["image_url"].startswith("data:image/png;base64,")
    assert "attachment_id" not in blocks[1]


def test_hydrate_image_blocks_drops_missing_attachment(setup):
    # If the attachment row is gone (session purged) or the file vanished,
    # silently drop the image block instead of crashing the whole run.
    conn, root, ag = setup
    history = [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "t"},
            {"type": "input_image", "attachment_id": "does-not-exist"},
        ],
    }]
    out = ag.hydrate_image_blocks(history, session_id="s1",
                                  data_root=str(root))
    blocks = out[0]["content"]
    assert blocks == [{"type": "input_text", "text": "t"}]


def test_hydrate_image_blocks_passthrough_for_image_url(setup):
    # Already-hydrated blocks (mid-turn, before storage compaction) must
    # be left untouched so the function is idempotent.
    conn, root, ag = setup
    history = [{
        "role": "user",
        "content": [{
            "type": "input_image",
            "image_url": "data:image/png;base64,AAAA",
        }],
    }]
    out = ag.hydrate_image_blocks(history, session_id="s1",
                                  data_root=str(root))
    assert out == history


def test_attachment_system_prompt_block(setup):
    conn, root, ag = setup
    _mk_att(conn, root, aid="t3", kind="text", mime="text/plain",
            filename="server.log", body=b"a" * 340_000)
    _mk_att(conn, root, aid="i3", kind="image", mime="image/png",
            filename="x.png")
    block = ag.attachment_system_block(["t3", "i3"], session_id="s1")
    assert "x.png" not in block
    assert "server.log" in block
    assert "read_attachment" in block
