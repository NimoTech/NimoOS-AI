import importlib
import phoenix_tracing as pt


def _reload(monkeypatch, **env):
    for k in ("NIMOOS_AGENT_TRACING", "PHOENIX_OTLP_ENDPOINT", "PHOENIX_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(pt)


def test_disabled_by_default(monkeypatch):
    m = _reload(monkeypatch)
    assert m.tracing_enabled() is False


def test_enabled_truthy_values(monkeypatch):
    for v in ("1", "true", "TRUE", "yes", "On"):
        m = _reload(monkeypatch, NIMOOS_AGENT_TRACING=v)
        assert m.tracing_enabled() is True, v


def test_endpoint_and_project_defaults(monkeypatch):
    m = _reload(monkeypatch)
    assert m.otlp_endpoint() == "http://127.0.0.1:6006/v1/traces"
    assert m.project_name() == "nimoos-agent"


def test_endpoint_and_project_override(monkeypatch):
    m = _reload(monkeypatch,
                PHOENIX_OTLP_ENDPOINT="http://172.17.0.1:6006/v1/traces",
                PHOENIX_PROJECT="custom")
    assert m.otlp_endpoint() == "http://172.17.0.1:6006/v1/traces"
    assert m.project_name() == "custom"


def test_setup_noop_when_disabled(monkeypatch):
    m = _reload(monkeypatch)
    # Must not raise and must not require OTel deps when disabled.
    assert m.setup_tracing() is False


def test_setup_swallows_exceptions(monkeypatch):
    m = _reload(monkeypatch, NIMOOS_AGENT_TRACING="1")
    # Force the instrumentation path to blow up; setup must swallow and return False.
    monkeypatch.setattr(m, "_install_processors", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert m.setup_tracing() is False


def test_run_config_none_when_disabled(monkeypatch):
    m = _reload(monkeypatch)
    assert m.build_trace_run_config("s1", "u1", "deepseek-x", "chat") is None


def test_run_config_built_when_enabled(monkeypatch):
    m = _reload(monkeypatch, NIMOOS_AGENT_TRACING="1")
    rc = m.build_trace_run_config("s1", "u1", "deepseek-x", "chat")
    assert rc is not None
    assert rc.group_id == "s1"
    assert rc.trace_metadata["user_id"] == "u1"
    assert rc.trace_metadata["model"] == "deepseek-x"
    assert rc.trace_metadata["agent_type"] == "chat"
