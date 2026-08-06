import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock

REGEN_HEADERS = {
    "X-User-Id": "1",
    "X-Agent-Provider-Key": "fake-key",
    "X-Agent-Provider-Url": "http://fake/v1",
}


async def _seed_history(client, sid, history_json):
    """Insert a history row directly into the test DB via the same connection."""
    import main as _main
    import json, time, uuid
    _main._conn.execute(
        "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), sid, "history", json.dumps(history_json), int(time.time())),
    )
    _main._conn.commit()


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

def test_clean_llm_title_strips_cjk_quote_pairs():
    from title_gen import clean_llm_title
    # Test CJK corner brackets
    assert clean_llm_title("「会话讨论」") == "会话讨论"
    assert clean_llm_title("『Project』") == "Project"
    # Test curly/smart quotes (U+201C and U+201D)
    assert clean_llm_title("“Smart quotes”") == "Smart quotes"


@pytest.mark.asyncio
async def test_regenerate_title_empty_model_returns_fallback(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    await _seed_history(client, sid, [{"role": "user", "content": "Hello there friend"}])
    resp = await client.post(
        f"/agent/sessions/{sid}/regenerate-title",
        headers=REGEN_HEADERS,
        json={"model": ""},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback"] is True
    assert body["title"] == "Hello there frie"


@pytest.mark.asyncio
async def test_regenerate_title_no_history_returns_fallback(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    resp = await client.post(
        f"/agent/sessions/{sid}/regenerate-title",
        headers=REGEN_HEADERS,
        json={"model": "gpt-4o-mini"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback"] is True
    assert body["title"] == ""


@pytest.mark.asyncio
async def test_regenerate_title_llm_success(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    await _seed_history(client, sid, [{"role": "user", "content": "Discuss project alpha plan"}])

    fake_choice = type("C", (), {"message": type("M", (), {"content": "\u9879\u76ee\u65b9\u6848\u8ba8\u8bba"})()})()
    fake_resp = type("R", (), {"choices": [fake_choice]})()

    with patch("main.AsyncOpenAI") as mock_client_cls:
        mock_create = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value.chat.completions.create = mock_create
        resp = await client.post(
            f"/agent/sessions/{sid}/regenerate-title",
            headers=REGEN_HEADERS,
            json={"model": "gpt-4o-mini"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback"] is False
    assert body["title"] == "\u9879\u76ee\u65b9\u6848\u8ba8\u8bba"
    listed = await client.get("/agent/sessions", headers={"X-User-Id": "1"})
    titles = [s["title"] for s in listed.json()]
    assert "\u9879\u76ee\u65b9\u6848\u8ba8\u8bba" in titles


@pytest.mark.asyncio
async def test_regenerate_title_llm_returns_quoted(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    await _seed_history(client, sid, [{"role": "user", "content": "Hello"}])

    fake_choice = type("C", (), {"message": type("M", (), {"content": "  \"Hello world\"\n"})()})()
    fake_resp = type("R", (), {"choices": [fake_choice]})()

    with patch("main.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = AsyncMock(return_value=fake_resp)
        resp = await client.post(
            f"/agent/sessions/{sid}/regenerate-title",
            headers=REGEN_HEADERS,
            json={"model": "gpt-4o-mini"},
        )
    assert resp.json()["title"] == "Hello world"


@pytest.mark.asyncio
async def test_regenerate_title_llm_timeout_falls_back(client):
    import asyncio
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    await _seed_history(client, sid, [{"role": "user", "content": "Test message body"}])

    with patch("main.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = AsyncMock(
            side_effect=asyncio.TimeoutError())
        resp = await client.post(
            f"/agent/sessions/{sid}/regenerate-title",
            headers=REGEN_HEADERS,
            json={"model": "gpt-4o-mini"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fallback"] is True
    assert body["title"] == "Test message bod"


@pytest.mark.asyncio
async def test_regenerate_title_history_truncated_to_2000(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    await _seed_history(client, sid, [{"role": "user", "content": "x" * 5000}])

    fake_choice = type("C", (), {"message": type("M", (), {"content": "Title"})()})()
    fake_resp = type("R", (), {"choices": [fake_choice]})()

    with patch("main.AsyncOpenAI") as mock_client_cls:
        mock_create = AsyncMock(return_value=fake_resp)
        mock_client_cls.return_value.chat.completions.create = mock_create
        await client.post(
            f"/agent/sessions/{sid}/regenerate-title",
            headers=REGEN_HEADERS,
            json={"model": "gpt-4o-mini"},
        )

    call_args = mock_create.call_args
    messages = call_args.kwargs["messages"]
    user_content = next(m["content"] for m in messages if m["role"] == "user")
    assert len(user_content) <= 2000


@pytest.mark.asyncio
async def test_regenerate_title_uses_reasoning_content_when_content_empty(client):
    r = await client.post("/agent/sessions", headers={"X-User-Id": "1"})
    sid = r.json()["session_id"]
    await _seed_history(client, sid, [{"role": "user", "content": "Reasoning model test"}])

    fake_choice = type(
        "C", (),
        {"message": type("M", (), {"content": "", "reasoning_content": "Reasoning model title"})()},
    )()
    fake_resp = type("R", (), {"choices": [fake_choice]})()

    with patch("main.AsyncOpenAI") as mock_client_cls:
        mock_client_cls.return_value.chat.completions.create = AsyncMock(return_value=fake_resp)
        resp = await client.post(
            f"/agent/sessions/{sid}/regenerate-title",
            headers=REGEN_HEADERS,
            json={"model": "deepseek-v4-flash"},
        )
    body = resp.json()
    assert body["fallback"] is False
    assert body["title"] == "Reasoning model title"
