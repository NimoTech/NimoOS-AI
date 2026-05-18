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


def test_function_tool_reads_via_context_vars(setup):
    """Regression: AgentRunner.run must set DATA_ROOT_VAR so the LLM-invoked
    @function_tool wrapper can resolve attachment paths."""
    conn, root, skill = setup
    _mk_att(conn, root, aid="ctx1", kind="text", mime="text/plain",
            content_bytes=b"hello via context", filename="x.txt")

    # Bind context vars the way AgentRunner.run does (post-fix).
    skill.SESSION_ID_VAR.set("s1")
    skill.USER_ID_VAR.set("u1")
    skill.MAX_CHARS_VAR.set(1024)
    skill.DATA_ROOT_VAR.set(str(root))
    skill.DB_VAR.set(conn)

    # Call the impl with no explicit conn/data_root so it falls back to vars.
    # (The @function_tool decorator wraps in an SDK-specific callable that's
    # awkward to invoke directly in tests; calling _impl with default kwargs
    # exercises the same fallback paths.)
    result = skill._read_attachment_impl(
        "ctx1",
        session_id=skill.SESSION_ID_VAR.get(),
        user_id=skill.USER_ID_VAR.get(),
        max_chars=skill.MAX_CHARS_VAR.get(),
    )
    assert result["kind"] == "text"
    assert result["content"] == "hello via context"


def test_document_with_sidecar_returns_content(setup, tmp_path):
    conn, root, skill = setup
    # Original file (bytes don't matter for read_attachment in document path,
    # but we keep one on disk to mirror real upload state).
    _mk_att(conn, root, aid="d1", kind="document", mime="application/pdf",
            content_bytes=b"%PDF-1.4\n", filename="r.pdf",
            meta={"sidecar": "d1__r.pdf.md", "extractor": "pypdf",
                  "pages": 4, "chars": 12, "truncated": False})
    # Drop sidecar next to the original.
    side = root / "sessions" / "s1" / "attachments" / "d1__r.pdf.md"
    side.write_text("# Title\nbody text", encoding="utf-8")

    result = skill._read_attachment_impl(
        "d1", session_id="s1", user_id="u1", max_chars=1000,
        conn=conn, data_root=str(root))
    assert result["kind"] == "document"
    assert result["content"] == "# Title\nbody text"
    assert result["extractor"] == "pypdf"
    assert result["pages"] == 4
    assert result["truncated"] is False


def test_document_with_extract_error_returns_error_field(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="d2", kind="document", mime="application/pdf",
            content_bytes=b"%PDF-1.4\n", filename="scan.pdf",
            meta={"extract_error": "empty_scanned"})
    result = skill._read_attachment_impl(
        "d2", session_id="s1", user_id="u1", max_chars=1000,
        conn=conn, data_root=str(root))
    assert result == {"kind": "document", "filename": "scan.pdf",
                      "mime": "application/pdf", "error": "empty_scanned",
                      "total_bytes": len(b"%PDF-1.4\n")}


def test_document_sidecar_missing_returns_vanished(setup):
    conn, root, skill = setup
    _mk_att(conn, root, aid="d3", kind="document", mime="application/pdf",
            content_bytes=b"%PDF-1.4\n", filename="r.pdf",
            meta={"sidecar": "d3__r.pdf.md", "extractor": "pypdf",
                  "pages": 1, "chars": 0, "truncated": False})
    # Sidecar deliberately NOT written.
    result = skill._read_attachment_impl(
        "d3", session_id="s1", user_id="u1", max_chars=1000,
        conn=conn, data_root=str(root))
    assert result == {"error": "vanished"}
