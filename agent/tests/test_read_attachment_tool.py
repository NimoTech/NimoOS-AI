import importlib
import json
import os
import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "agent.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    import db as db_module
    importlib.reload(db_module)
    conn = db_module.init_db(db_path, snapshots_root=str(tmp_path / "snaps"))
    import skills.attachments as att_skill
    importlib.reload(att_skill)
    conn.execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
        ("s1", "u1"))
    conn.commit()
    return conn, tmp_path, att_skill


def _mk_att(conn, root, *, aid, kind, mime, content_bytes, filename, meta=None,
            session_id="s1"):
    rel = f"{aid}__{filename}"
    p = root / "sessions" / session_id / "attachments"
    p.mkdir(parents=True, exist_ok=True)
    (p / rel).write_bytes(content_bytes)
    conn.execute(
        "INSERT INTO attachments "
        "(id,session_id,message_id,filename,mime,kind,size_bytes,rel_path,"
        " meta_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, session_id, "m1", filename, mime, kind, len(content_bytes), rel,
         json.dumps(meta) if meta else None, 0))
    conn.commit()


def test_text_returns_content(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="a1", kind="text", mime="text/plain",
            content_bytes=b"hello world", filename="x.txt")
    result = skill._read_attachment_impl(
        "a1", session_id="s1", user_id="u1", max_chars=100,
        conn=conn, data_root=str(root))
    assert result["kind"] == "text"
    assert result["content"] == "hello world"
    assert result["truncated"] is False


def test_text_truncated_by_chars_safe_for_multibyte(setup):
    conn, root, skill = setup
    body = ("中" * 50).encode("utf-8")  # 150 bytes
    _mk_att(conn, root, aid="a2", kind="text", mime="text/plain",
            content_bytes=body, filename="cn.txt")
    result = skill._read_attachment_impl(
        "a2", session_id="s1", user_id="u1", max_chars=10,
        conn=conn, data_root=str(root))
    assert result["truncated"] is True
    assert len(result["content"]) == 10
    assert "�" not in result["content"]
    assert result["content"] == "中" * 10


def test_video_returns_metadata(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="a3", kind="video", mime="video/mp4",
            content_bytes=b"\x00" * 32, filename="x.mp4",
            meta={"duration": 12.5, "codec": "h264"})
    result = skill._read_attachment_impl(
        "a3", session_id="s1", user_id="u1", max_chars=100,
        conn=conn, data_root=str(root))
    assert result["kind"] == "video"
    assert result["metadata"]["duration"] == 12.5
    assert "content" not in result


def test_binary_returns_note(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="a4", kind="binary", mime="application/x-binary",
            content_bytes=b"\x00\x01\x02", filename="x.bin")
    result = skill._read_attachment_impl(
        "a4", session_id="s1", user_id="u1", max_chars=100,
        conn=conn, data_root=str(root))
    assert result["kind"] == "binary"
    assert "note" in result


def test_image_returns_already_visible(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="a5", kind="image", mime="image/png",
            content_bytes=b"\x89PNG", filename="x.png")
    result = skill._read_attachment_impl(
        "a5", session_id="s1", user_id="u1", max_chars=100,
        conn=conn, data_root=str(root))
    assert result.get("error") == "image_already_visible"


def test_cross_session_returns_not_found(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="a6", kind="text", mime="text/plain",
            content_bytes=b"x", filename="x.txt")
    result = skill._read_attachment_impl(
        "a6", session_id="s2", user_id="u1", max_chars=100,
        conn=conn, data_root=str(root))
    assert result.get("error") == "not_found"


def test_file_vanished(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="a7", kind="text", mime="text/plain",
            content_bytes=b"x", filename="x.txt")
    os.remove(root / "sessions/s1/attachments/a7__x.txt")
    result = skill._read_attachment_impl(
        "a7", session_id="s1", user_id="u1", max_chars=100,
        conn=conn, data_root=str(root))
    assert result.get("error") == "vanished"
