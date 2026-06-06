import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.asyncio


def _mock_async_client(resp):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)
    client.patch = AsyncMock(return_value=resp)
    return client


def _url_file(tmp_path):
    f = tmp_path / "photos.url"
    f.write_text("http://127.0.0.1:59999")
    return str(f)


async def test_list_albums_compact_fields(tmp_path):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {"id": "al1", "name": "Untitled", "assetCount": 42,
         "dateStart": "2024-04-02 10:00:00", "dateEnd": "2024-04-09 12:00:00",
         "coverAssetId": "x", "photoCount": 40, "videoCount": 2},
    ]
    import skills.photos as m
    with patch("skills.photos.URL_FILE", _url_file(tmp_path)), \
         patch("httpx.AsyncClient", return_value=_mock_async_client(resp)):
        out = json.loads(await m.list_albums.on_invoke_tool(MagicMock(), "{}"))
    assert out["count"] == 1
    a = out["albums"][0]
    assert a == {"id": "al1", "name": "Untitled", "assetCount": 42,
                 "dateStart": "2024-04-02", "dateEnd": "2024-04-09"}


async def test_list_albums_service_unavailable(tmp_path):
    import skills.photos as m
    with patch("skills.photos.URL_FILE", str(tmp_path / "missing.url")):
        out = await m.list_albums.on_invoke_tool(MagicMock(), "{}")
        assert "unavailable" in out.lower()


async def test_get_album_summary_passthrough(tmp_path):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"assetCount": 4, "topPlaces": [{"city": "Tokyo"}]}
    import skills.photos as m
    with patch("skills.photos.URL_FILE", _url_file(tmp_path)), \
         patch("httpx.AsyncClient", return_value=_mock_async_client(resp)) as cls:
        out = json.loads(await m.get_album_summary.on_invoke_tool(
            MagicMock(), '{"album_id": "al1"}'))
    assert out["topPlaces"][0]["city"] == "Tokyo"
    called_url = cls.return_value.get.call_args[0][0]
    assert called_url.endswith("/v1/photos/albums/al1/summary")


async def test_get_album_summary_not_found(tmp_path):
    resp = MagicMock()
    resp.status_code = 404
    import skills.photos as m
    with patch("skills.photos.URL_FILE", _url_file(tmp_path)), \
         patch("httpx.AsyncClient", return_value=_mock_async_client(resp)):
        out = await m.get_album_summary.on_invoke_tool(MagicMock(), '{"album_id": "x"}')
        assert "not found" in out.lower()


async def test_rename_album_success(tmp_path):
    resp = MagicMock()
    resp.status_code = 200
    import skills.photos as m
    with patch("skills.photos.URL_FILE", _url_file(tmp_path)), \
         patch("httpx.AsyncClient", return_value=_mock_async_client(resp)) as cls:
        out = await m.rename_album.on_invoke_tool(
            MagicMock(), '{"album_id": "al1", "new_name": "Tokyo · April 2024"}')
    assert "Tokyo · April 2024" in out
    kwargs = cls.return_value.patch.call_args
    assert kwargs[0][0].endswith("/v1/photos/albums/al1")
    assert kwargs[1]["json"] == {"name": "Tokyo · April 2024"}


async def test_rename_album_conflict(tmp_path):
    resp = MagicMock()
    resp.status_code = 409
    import skills.photos as m
    with patch("skills.photos.URL_FILE", _url_file(tmp_path)), \
         patch("httpx.AsyncClient", return_value=_mock_async_client(resp)):
        out = await m.rename_album.on_invoke_tool(
            MagicMock(), '{"album_id": "al1", "new_name": "Dup"}')
        assert "already exists" in out


async def test_look_at_photos_vision_unsupported(tmp_path):
    import skills.photos as m
    m.VISION_CFG_VAR.set({"ok": False})
    with patch("httpx.AsyncClient") as cls:
        out = await m.look_at_photos.on_invoke_tool(
            MagicMock(), '{"asset_ids": ["p1"]}')
    assert "does not support vision" in out
    cls.assert_not_called()  # zero requests


async def test_look_at_photos_describes(tmp_path):
    thumb = MagicMock()
    thumb.status_code = 200
    thumb.content = b"\xff\xd8fakejpeg"
    thumb.headers = {"content-type": "image/jpeg"}

    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = "1. A sunset over the sea."

    oai = MagicMock()
    oai.chat.completions.create = AsyncMock(return_value=completion)

    import skills.photos as m
    m.VISION_CFG_VAR.set({"ok": True, "base_url": "http://llm",
                          "api_key": "k", "model": "qwen-vl"})
    with patch("skills.photos.URL_FILE", _url_file(tmp_path)), \
         patch("httpx.AsyncClient", return_value=_mock_async_client(thumb)), \
         patch("skills.photos.AsyncOpenAI", return_value=oai) as oai_cls:
        out = await m.look_at_photos.on_invoke_tool(
            MagicMock(), '{"asset_ids": ["p1", "p2", "p3", "p4"]}')

    assert "sunset" in out
    oai_cls.assert_called_once_with(base_url="http://llm", api_key="k")
    msgs = oai.chat.completions.create.call_args[1]["messages"]
    parts = msgs[0]["content"]
    # 1 text instruction + up to 3 images (4th asset id is truncated)
    assert parts[0]["type"] == "text"
    assert len([p for p in parts if p["type"] == "image_url"]) == 3
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert oai.chat.completions.create.call_args[1]["model"] == "qwen-vl"


async def test_look_at_photos_all_thumbs_fail(tmp_path):
    thumb = MagicMock()
    thumb.status_code = 404
    import skills.photos as m
    m.VISION_CFG_VAR.set({"ok": True, "base_url": "http://llm",
                          "api_key": "k", "model": "qwen-vl"})
    with patch("skills.photos.URL_FILE", _url_file(tmp_path)), \
         patch("httpx.AsyncClient", return_value=_mock_async_client(thumb)):
        out = await m.look_at_photos.on_invoke_tool(
            MagicMock(), '{"asset_ids": ["p1"]}')
    assert "Could not load" in out


async def test_photos_profile_has_seven_tools():
    from profiles import PROFILES
    assert len(PROFILES["photos"].tools) == 7


async def test_photos_prompt_mentions_album_workflow():
    from profiles import PHOTOS_SYSTEM_PROMPT as p
    assert "list_albums" in p
    assert "get_album_summary" in p
    assert "rename_album" in p
    assert "look_at_photos" in p
    assert "old name → new name" in p  # final report requirement
