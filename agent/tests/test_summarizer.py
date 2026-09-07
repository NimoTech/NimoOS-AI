import asyncio
import pytest

import summarizer as sm
from db import init_db


class _Msg:  # fake chat completion
    def __init__(self, text): self.content = text
class _Choice:
    def __init__(self, text): self.message = _Msg(text)
class _Resp:
    def __init__(self, text): self.choices = [_Choice(text)]


class FakeClient:
    def __init__(self, text="SUM", label=""):
        self.text, self.label, self.calls, self.closed = text, label, [], False
        outer = self
        class _C:
            async def create(self_inner, **kw):
                outer.calls.append(kw); return _Resp(outer.text)
        class _Chat: completions = _C()
        self.chat = _Chat()
    async def close(self): self.closed = True


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "s.db"))
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('s1','u1',0,0)"); c.commit()
    return c


@pytest.mark.asyncio
async def test_session_summarize_fn_shape():
    fc = FakeClient("  ROLLED ")
    fn = sm.session_summarize_fn(fc, "qwen")
    assert await fn("INSTR", "PRIOR", "FOLD") == "ROLLED"
    kw = fc.calls[0]
    assert kw["model"] == "qwen" and kw["messages"][0]["role"] == "system" and "PRIOR" in kw["messages"][1]["content"]


@pytest.mark.asyncio
async def test_prefers_background_model_when_configured(conn, monkeypatch):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) VALUES('u1','background_model','cloud:4:flash',0)"); conn.commit()
    bg = FakeClient("BG")
    async def fake_resolve(user_id, model):
        assert (user_id, model) == ("u1", "cloud:4:flash")
        return {"base_url": "http://bg", "api_key": "k", "model": "flash", "provider_type": "deepseek"}
    monkeypatch.setattr(sm, "_new_client", lambda base_url, api_key: bg)
    session = FakeClient("SESSION")
    fn = sm.make_summarizer(conn, "u1", session, "qwen", creds_resolver=fake_resolve)
    assert await fn("I", "P", "F") == "BG"
    assert bg.calls[0]["model"] == "flash" and session.calls == []
    await fn.aclose()
    assert bg.closed


@pytest.mark.asyncio
async def test_falls_back_to_session_client_without_background_model(conn):
    session = FakeClient("SESSION")
    fn = sm.make_summarizer(conn, "u1", session, "qwen")
    assert await fn("I", "P", "F") == "SESSION"
    await fn.aclose()


@pytest.mark.asyncio
async def test_falls_back_when_resolution_fails(conn, monkeypatch):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) VALUES('u1','background_model','cloud:4:flash',0)"); conn.commit()
    async def bad_resolve(user_id, model): raise RuntimeError("go down")
    session = FakeClient("SESSION")
    fn = sm.make_summarizer(conn, "u1", session, "qwen", creds_resolver=bad_resolve)
    assert await fn("I", "P", "F") == "SESSION"


@pytest.mark.asyncio
async def test_ollama_background_disables_thinking(conn, monkeypatch):
    conn.execute("INSERT INTO user_settings(user_id,key,value,updated_at) VALUES('u1','background_model','qwen3:8b',0)"); conn.commit()
    bg = FakeClient("BG")
    async def fake_resolve(user_id, model):
        return {"base_url": "http://ollama", "api_key": "", "model": "qwen3:8b", "provider_type": "ollama"}
    monkeypatch.setattr(sm, "_new_client", lambda base_url, api_key: bg)
    fn = sm.make_summarizer(conn, "u1", FakeClient(), "x", creds_resolver=fake_resolve)
    await fn("I", "P", "F")
    assert "extra_body" in bg.calls[0]


@pytest.mark.asyncio
async def test_timeout_returns_empty(conn, monkeypatch):
    monkeypatch.setattr(sm, "COMPACT_LLM_TIMEOUT", 0.05)
    class Slow(FakeClient):
        def __init__(self):
            super().__init__()
            outer = self
            class _C:
                async def create(self_inner, **kw):
                    await asyncio.sleep(1); return _Resp("late")
            class _Chat: completions = _C()
            self.chat = _Chat()
    fn = sm.make_summarizer(conn, "u1", Slow(), "x")
    assert await fn("I", "P", "F") == ""
