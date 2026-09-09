"""aux_thinking_kwargs: shared thinking-disable rule for auxiliary one-shot
calls (background summarizer client and the session-fallback client used
when background_model is unset). See summarizer.py docstring."""
import pytest

import summarizer as sm


class _Msg:  # fake chat completion
    def __init__(self, text): self.content = text
class _Choice:
    def __init__(self, text): self.message = _Msg(text)
class _Resp:
    def __init__(self, text): self.choices = [_Choice(text)]


class FakeClient:
    def __init__(self, text="SESSION"):
        self.text, self.calls = text, []
        outer = self
        class _C:
            async def create(self_inner, **kw):
                outer.calls.append(kw); return _Resp(outer.text)
        class _Chat: completions = _C()
        self.chat = _Chat()


ARK_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"
LAN_URL = "http://192.168.1.183:8081/v1"


def test_other_ark_disables_thinking():
    assert sm.aux_thinking_kwargs("other", ARK_URL) == {"extra_body": {"thinking": {"type": "disabled"}}}


def test_other_non_ark_is_noop():
    assert sm.aux_thinking_kwargs("other", LAN_URL) == {}


def test_other_no_base_url_is_noop():
    assert sm.aux_thinking_kwargs("other", "") == {}


def test_deepseek_disables_thinking():
    kw = sm.aux_thinking_kwargs("deepseek", "")
    assert kw["extra_body"]["thinking"]["type"] == "disabled"


@pytest.mark.parametrize("provider_type", ["qwen", "ollama"])
def test_qwen_ollama_match_prior_background_behavior(provider_type):
    kw = sm.aux_thinking_kwargs(provider_type, "http://bg:11434/v1")
    assert kw.get("extra_body")


def test_never_raises_on_bogus_provider_type():
    assert sm.aux_thinking_kwargs("not-a-real-provider", "http://x") == {}


@pytest.fixture
def conn(tmp_path):
    from db import init_db
    c = init_db(str(tmp_path / "s.db"))
    c.execute("INSERT INTO sessions(id,user_id,created_at,updated_at) VALUES('s1','u1',0,0)"); c.commit()
    return c


@pytest.mark.asyncio
async def test_make_summarizer_fallback_disables_thinking_on_ark(conn):
    session = FakeClient()
    fn = sm.make_summarizer(conn, "u1", session, "doubao-seed-2-1-turbo",
                             provider_type="other", base_url=ARK_URL)
    await fn("I", "P", "F")
    assert session.calls[0].get("extra_body") == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_make_summarizer_fallback_leaves_non_ark_untouched(conn):
    session = FakeClient()
    fn = sm.make_summarizer(conn, "u1", session, "some-model",
                             provider_type="other", base_url=LAN_URL)
    await fn("I", "P", "F")
    assert "extra_body" not in session.calls[0]
