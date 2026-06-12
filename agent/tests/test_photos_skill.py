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


async def test_search_photos_rejects_non_english_query(tmp_path):
    url_file = tmp_path / "photos.url"
    url_file.write_text("http://127.0.0.1:59999")
    import skills.photos as m
    with patch("skills.photos.URL_FILE", str(url_file)):
        result = await m.search_photos.on_invoke_tool(
            MagicMock(), '{"query": "海边日落"}')
        assert "English only" in result


async def test_search_photos_ocr_text_merges_on_top(tmp_path):
    url_file = tmp_path / "photos.url"
    url_file.write_text("http://127.0.0.1:59999")

    visual_resp = MagicMock()
    visual_resp.status_code = 200
    visual_resp.json.return_value = [
        {"id": "v1", "originalName": "shop.jpg", "takenAt": "2025-01-01T00:00:00Z"},
        {"id": "dup", "originalName": "both.jpg", "takenAt": "2025-01-02T00:00:00Z"},
    ]
    ocr_resp = MagicMock()
    ocr_resp.status_code = 200
    ocr_resp.json.return_value = [
        {"id": "o1", "originalName": "receipt.jpg",
         "takenAt": "2025-02-01T00:00:00Z", "matchedBy": "ocr"},
        {"id": "dup", "originalName": "both.jpg",
         "takenAt": "2025-01-02T00:00:00Z", "matchedBy": "ocr"},
        # CLIP tail of the non-English query must be dropped
        {"id": "noise", "originalName": "noise.jpg", "takenAt": ""},
    ]

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=[visual_resp, ocr_resp])

    import json as _json
    import skills.photos as m
    with patch("skills.photos.URL_FILE", str(url_file)), \
         patch("httpx.AsyncClient", return_value=client):
        result = await m.search_photos.on_invoke_tool(
            MagicMock(), '{"query": "computer receipt", "ocr_text": "发票"}')
    data = _json.loads(result)
    ids = [r["id"] for r in data["results"]]
    # OCR hits first, deduped against visual results, CLIP noise dropped
    assert ids == ["o1", "dup", "v1"]
    assert data["results"][0]["matchedBy"] == "ocr"
    assert "noise" not in ids
    # second request carried the original-language keyword
    ocr_payload = client.post.call_args_list[1].kwargs["json"]
    assert ocr_payload["query"] == "发票"
