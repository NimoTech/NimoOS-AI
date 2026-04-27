import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    import importlib, sys
    for mod in ["main", "agent", "db", "title_gen"]:
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c

@pytest.mark.asyncio
async def test_patch_title_happy_path(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    resp = await client.patch(
        f"/agent/sessions/{sid}/title",
        headers={"X-User-Id": "1"},
        json={"title": "Project Alpha"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Project Alpha"
    assert "updated_at" in body

    listed = await client.get("/agent/sessions", headers={"X-User-Id": "1"})
    titles = [s["title"] for s in listed.json()]
    assert "Project Alpha" in titles


@pytest.mark.asyncio
async def test_patch_title_empty_rejected(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    resp = await client.patch(
        f"/agent/sessions/{sid}/title",
        headers={"X-User-Id": "1"},
        json={"title": "   "},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_title_wrong_user_404(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    resp = await client.patch(
        f"/agent/sessions/{sid}/title",
        headers={"X-User-Id": "2"},
        json={"title": "stolen"},
    )
    assert resp.status_code == 404


def test_extract_history_excerpt_truncates_to_2000():
    from title_gen import extract_history_excerpt
    history = [
        {"role": "user", "content": "x" * 5000},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "y" * 5000}]},
    ]
    excerpt = extract_history_excerpt(history)
    assert len(excerpt) <= 2000

def test_extract_history_excerpt_picks_first_six_textual():
    from title_gen import extract_history_excerpt
    history = []
    for i in range(10):
        history.append({"role": "user", "content": f"u{i}"})
        history.append({"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": f"a{i}"}]})
    excerpt = extract_history_excerpt(history)
    # 6 items max, in order
    assert "u0" in excerpt and "a2" in excerpt
    assert "u4" not in excerpt and "a4" not in excerpt

def test_clean_llm_title_strips_quotes_newlines_and_truncates():
    from title_gen import clean_llm_title
    assert clean_llm_title('  "Hello world"\n\n') == "Hello world"
    assert clean_llm_title("a" * 100) == "a" * 30
    assert clean_llm_title("\n\n") == ""

def test_first_user_message_fallback():
    from title_gen import first_user_fallback
    history = [{"role": "user", "content": "Hello, this is a long message"}]
    assert first_user_fallback(history) == "Hello, this is a "[:16]

def test_first_user_message_fallback_empty():
    from title_gen import first_user_fallback
    assert first_user_fallback([]) == ""
