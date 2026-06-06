import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.asyncio


def _mock_async_client(resp):
    """Build a mock for `async with httpx.AsyncClient(...) as client`."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)
    client.patch = AsyncMock(return_value=resp)
    return client


async def test_search_photos_service_unavailable(tmp_path):
    bad_url_file = str(tmp_path / "missing.url")  # file does not exist
    import skills.photos as m
    with patch("skills.photos.URL_FILE", bad_url_file):
        result = await m.search_photos.on_invoke_tool(MagicMock(), '{"query": "beach"}')
        assert "unavailable" in result.lower()


async def test_search_photos_returns_results(tmp_path):
    url_file = tmp_path / "photos.url"
    url_file.write_text("http://127.0.0.1:59999")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {"id": "abc123", "originalName": "beach.jpg", "takenAt": "2025-07-15T10:00:00Z"}
    ]

    import skills.photos as m
    with patch("skills.photos.URL_FILE", str(url_file)), \
         patch("httpx.AsyncClient", return_value=_mock_async_client(resp)):
        result = await m.search_photos.on_invoke_tool(MagicMock(), '{"query": "beach"}')
        assert "beach.jpg" in result
        assert "2025-07-15" in result
