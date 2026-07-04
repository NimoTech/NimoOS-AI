import pytest
from skills.send_attachment import _send_attachment_impl


@pytest.mark.asyncio
async def test_send_ok_returns_real_success():
    sent = {}
    async def fake_send(path, caption): sent["p"] = path; return "mid42"
    out = await _send_attachment_impl("/DATA/x.txt", "hi", send_file=fake_send,
                                      validate=lambda p: "/DATA/x.txt")
    assert "mid42" in out and sent["p"] == "/DATA/x.txt"


@pytest.mark.asyncio
async def test_send_rejects_out_of_gate():
    out = await _send_attachment_impl("/etc/passwd", "", send_file=None,
                                      validate=lambda p: None)  # 门控拒
    assert "error" in out.lower() or "拒" in out or "not allowed" in out.lower()


@pytest.mark.asyncio
async def test_send_failure_surfaces_to_model():
    async def boom(path, caption): raise RuntimeError("network down")
    out = await _send_attachment_impl("/DATA/x.txt", "", send_file=boom,
                                      validate=lambda p: "/DATA/x.txt")
    assert "network down" in out or "fail" in out.lower() or "失败" in out
