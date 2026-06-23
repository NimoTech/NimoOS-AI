import pytest
from unittest.mock import AsyncMock, MagicMock

from skills import photos as photos_skill


@pytest.mark.asyncio
async def test_describe_image_returns_text(monkeypatch):
    photos_skill.VISION_CFG_VAR.set(
        {"ok": True, "base_url": "http://x", "api_key": "k", "model": "m"})

    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content="a chart"))]
    fake_oai = MagicMock()
    fake_oai.chat.completions.create = AsyncMock(return_value=completion)
    monkeypatch.setattr(photos_skill, "AsyncOpenAI", lambda **kw: fake_oai)

    desc, err = await photos_skill.describe_image("BASE64", "what is this?")
    assert err is None
    assert desc == "a chart"
    # the image was sent as a data URL block
    sent = fake_oai.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert any(b.get("type") == "image_url" and
               b["image_url"]["url"].startswith("data:image/png;base64,BASE64")
               for b in sent)


@pytest.mark.asyncio
async def test_describe_image_no_vision(monkeypatch):
    photos_skill.VISION_CFG_VAR.set({"ok": False})
    desc, err = await photos_skill.describe_image("B", "q")
    assert desc == ""
    assert err is not None
