import httpx
import pytest
from unittest.mock import patch

from parser_client import ParserClient


@pytest.mark.asyncio
async def test_extract_posts_correct_shape(tmp_path):
    url_file = tmp_path / "parser.url"
    url_file.write_text("http://127.0.0.1:8283\n")

    async def fake_post(url, json=None, headers=None):
        class R:
            status_code = 200
            def json(self): return {"path": json["path"], "markdown": "md",
                                     "truncated": False, "ocr": json["ocr"]}
            def raise_for_status(self): pass
        assert url == "http://127.0.0.1:8283/v1/parser/extract"
        assert json == {"path": "/DATA/x.pdf", "ocr": True, "max_chars": 24000}
        assert headers == {"X-NimoOS-User-ID": "u1"}
        return R()

    client = ParserClient(discovery_path=str(url_file))
    with patch.object(client._client, "post", side_effect=fake_post):
        out = await client.extract("/DATA/x.pdf", ocr=True, max_chars=24000, user_id="u1")
    assert out["markdown"] == "md"
    await client.aclose()


@pytest.mark.asyncio
async def test_extract_raises_with_body_on_error(tmp_path):
    url_file = tmp_path / "parser.url"
    url_file.write_text("http://127.0.0.1:8283\n")
    request = httpx.Request("POST", "http://127.0.0.1:8283/v1/parser/extract")
    response = httpx.Response(403, request=request,
                              json={"detail": "path outside allowed roots"})

    async def fake_post(url, json=None, headers=None):
        return response

    client = ParserClient(discovery_path=str(url_file))
    with patch.object(client._client, "post", side_effect=fake_post):
        with pytest.raises(httpx.HTTPStatusError) as ei:
            await client.extract("/etc/shadow", user_id="u1")
    assert "path outside allowed roots" in str(ei.value)
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_discovery_file_raises(tmp_path):
    client = ParserClient(discovery_path=str(tmp_path / "nope.url"))
    with pytest.raises(RuntimeError):
        await client.extract("/DATA/x.pdf")
    await client.aclose()
