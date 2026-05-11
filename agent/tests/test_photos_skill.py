import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.asyncio


async def test_search_photos_service_unavailable(tmp_path):
    bad_url_file = str(tmp_path / "missing.url")  # file does not exist
    with patch("skills.photos.URL_FILE", bad_url_file):
        import importlib
        import skills.photos as m
        importlib.reload(m)
        result = await m.search_photos("beach")
        assert "unavailable" in result.lower()


async def test_search_photos_returns_results(tmp_path):
    url_file = tmp_path / "photos.url"
    url_file.write_text("http://127.0.0.1:59999")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"id": "abc123", "originalName": "beach.jpg", "takenAt": "2025-07-15T10:00:00Z"}
    ]

    with patch("skills.photos.URL_FILE", str(url_file)):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = MagicMock(return_value=mock_client)
            mock_client.__aexit__ = MagicMock(return_value=False)
            mock_client.post = MagicMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            import importlib
            import skills.photos as m
            importlib.reload(m)
            result = await m.search_photos("beach")
            assert "beach.jpg" in result
            assert "2025-07-15" in result
