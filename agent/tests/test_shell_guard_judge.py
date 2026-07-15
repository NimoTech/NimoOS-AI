import asyncio
import shell_guard.judge as J


def test_allow_verdict(monkeypatch):
    def fake_call(url, model, prompt, timeout):
        return {"response": '{"verdict":"allow","reason":"read only"}'}
    monkeypatch.setattr(J, "_call_ollama_sync", fake_call)
    assert asyncio.run(J.judge_command("cp a b")) == "allow"


def test_unknown_verdict_fails_to_ask(monkeypatch):
    def fake_call(url, model, prompt, timeout):
        return {"response": '{"verdict":"maybe"}'}
    monkeypatch.setattr(J, "_call_ollama_sync", fake_call)
    assert asyncio.run(J.judge_command("cp a b")) == "ask"


def test_connection_error_fails_to_ask(monkeypatch):
    import urllib.error
    def boom(url, model, prompt, timeout):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(J, "_call_ollama_sync", boom)
    assert asyncio.run(J.judge_command("cp a b")) == "ask"


def test_bad_json_fails_to_ask(monkeypatch):
    def fake_call(url, model, prompt, timeout):
        return {"response": "not json"}
    monkeypatch.setattr(J, "_call_ollama_sync", fake_call)
    assert asyncio.run(J.judge_command("cp a b")) == "ask"
