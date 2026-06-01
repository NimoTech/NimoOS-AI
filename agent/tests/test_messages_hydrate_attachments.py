import importlib
import json
import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "agent.db")
    monkeypatch.setenv("AGENT_DB_PATH", db_path)
    import db as db_module
    importlib.reload(db_module)
    db_module.init_db(db_path, snapshots_root=str(tmp_path / "snaps"))
    import agent as ag_module
    importlib.reload(ag_module)
    import main as main_module
    importlib.reload(main_module)
    return ag_module, main_module


def test_compact_image_block_for_storage(setup):
    ag, _ = setup
    history = [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "hi"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        ],
    }]
    compacted = ag.compact_image_blocks(
        history, image_id_resolver=lambda url: "att_xyz")
    msg = compacted[0]["content"]
    img = next(c for c in msg if c["type"] == "input_image")
    assert "image_url" not in img
    assert img["attachment_id"] == "att_xyz"


def test_compact_unresolved_url_left_unchanged(setup):
    ag, _ = setup
    history = [{
        "role": "user",
        "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,XYZ"},
        ],
    }]
    compacted = ag.compact_image_blocks(history, image_id_resolver=lambda u: None)
    img = compacted[0]["content"][0]
    assert img.get("image_url") == "data:image/png;base64,XYZ"


def test_hydrate_replaces_attachment_id_with_url(setup):
    _, m = setup
    history = [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "hi"},
            {"type": "input_image", "attachment_id": "att_xyz"},
        ],
    }]
    hydrated = m._hydrate_messages(history, session_id_for_urls="s1")
    user = next(h for h in hydrated if h["role"] == "user")
    # Verify the attachment_id and URL are somewhere in the user message
    blob = json.dumps(user)
    assert "att_xyz" in blob
    assert "/v1/ai/agent/sessions/s1/attachments/att_xyz/raw" in blob
