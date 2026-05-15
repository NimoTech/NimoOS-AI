import importlib
import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "agent.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    monkeypatch.setenv("NIMOOS_AGENT_DATA_ROOT", str(tmp_path))
    import db as db_module
    importlib.reload(db_module)
    conn = db_module.init_db(db_path, snapshots_root=str(tmp_path / "snaps"))
    db_module._conn = conn
    import agent as ag_module
    importlib.reload(ag_module)
    conn.execute(
        "INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES (?,?,0,0)",
        ("s1", "u1"))
    conn.commit()
    return conn, tmp_path, ag_module


def _mk_image(conn, root, aid="i1"):
    rel = f"{aid}__x.png"
    p = root / "sessions" / "s1" / "attachments"
    p.mkdir(parents=True, exist_ok=True)
    (p / rel).write_bytes(b"\x89PNG\x00")
    conn.execute(
        "INSERT INTO attachments "
        "(id,session_id,message_id,filename,mime,kind,size_bytes,rel_path,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, "s1", "m1", "x.png", "image/png", "image", 5, rel, 0))
    conn.commit()


def test_vision_model_uses_input_image(setup):
    conn, root, ag = setup
    _mk_image(conn, root)
    content = ag.build_user_content("hi", ["i1"], session_id="s1",
                                    data_root=str(root),
                                    model_name="gpt-4o", provider_type="openai")
    assert any(c["type"] == "input_image" for c in content)


def test_nonvision_model_falls_back_to_text(setup):
    conn, root, ag = setup
    _mk_image(conn, root)
    content = ag.build_user_content("hi", ["i1"], session_id="s1",
                                    data_root=str(root),
                                    model_name="llama3:8b", provider_type="ollama")
    assert all(c["type"] != "input_image" for c in content)
    text_blocks = [c["text"] for c in content if c["type"] == "input_text"]
    joined = " ".join(text_blocks)
    assert "x.png" in joined
    assert "image" in joined.lower()


def test_vision_capability_lookup():
    import provider_adapters as pa
    assert pa.model_supports_vision("openai", "gpt-4o") is True
    assert pa.model_supports_vision("anthropic", "claude-3-5-sonnet-20241022") is True
    assert pa.model_supports_vision("ollama", "llama3:8b") is False
    assert pa.model_supports_vision("ollama", "anything") is False
    assert pa.model_supports_vision("other", "anything") is True
