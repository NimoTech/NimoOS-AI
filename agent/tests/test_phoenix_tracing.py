import importlib
import phoenix_tracing as pt


def test_tracing_enabled_now_default_false(monkeypatch):
    monkeypatch.delenv("NIMOOS_AGENT_TRACING", raising=False)
    m = importlib.reload(pt)
    assert m.tracing_enabled_now() is False


def test_set_flag_toggles(monkeypatch):
    m = importlib.reload(pt)
    m._set_flag(True)
    assert m.tracing_enabled_now() is True
    m._set_flag(False)
    assert m.tracing_enabled_now() is False


class _RecordingExporter:
    def __init__(self):
        self.calls = 0
    def export(self, spans):
        self.calls += 1
        from opentelemetry.sdk.trace.export import SpanExportResult
        return SpanExportResult.SUCCESS
    def shutdown(self):
        pass
    def force_flush(self, timeout_millis=30000):
        return True


def test_gated_exporter_drops_when_disabled(monkeypatch):
    from opentelemetry.sdk.trace.export import SpanExportResult
    m = importlib.reload(pt)
    inner = _RecordingExporter()
    enabled = {"v": False}
    gated = m.GatedSpanExporter(inner, lambda: enabled["v"])
    assert gated.export(["span"]) == SpanExportResult.SUCCESS
    assert inner.calls == 0                      # disabled → no network call
    enabled["v"] = True
    assert gated.export(["span"]) == SpanExportResult.SUCCESS
    assert inner.calls == 1                       # enabled → delegates to inner exporter


def test_setup_tracing_opt_out(monkeypatch):
    monkeypatch.setenv("NIMOOS_AGENT_TRACING", "0")
    m = importlib.reload(pt)
    assert m.setup_tracing() is False             # explicitly off → not installed


def test_setup_tracing_installs(monkeypatch):
    monkeypatch.delenv("NIMOOS_AGENT_TRACING", raising=False)
    m = importlib.reload(pt)
    assert m.setup_tracing() is True              # deps installed → setup succeeds


def test_run_config_enabled(monkeypatch):
    import importlib, phoenix_tracing as pt
    m = importlib.reload(pt)
    rc = m.build_trace_run_config(True, "s1", "u1", "deepseek-x", "chat")
    assert rc.group_id == "s1"
    assert rc.workflow_name == "nimoos-agent"
    assert rc.trace_metadata["user_id"] == "u1"
    assert rc.tracing_disabled is False


def test_run_config_disabled(monkeypatch):
    import importlib, phoenix_tracing as pt
    m = importlib.reload(pt)
    rc = m.build_trace_run_config(False, "s1", "u1", "deepseek-x", "chat")
    assert rc.tracing_disabled is True


def test_setup_tracing_instruments_openai_client(monkeypatch):
    # The OpenAI client instrumentor must be wired to the same provider so the
    # enable/disable gate covers its spans too.
    monkeypatch.delenv("NIMOOS_AGENT_TRACING", raising=False)
    from openinference.instrumentation.openai import OpenAIInstrumentor
    calls = []

    def fake_instrument(self, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(OpenAIInstrumentor, "instrument", fake_instrument)
    m = importlib.reload(pt)
    assert m.setup_tracing() is True
    assert len(calls) == 1
    assert calls[0].get("tracer_provider") is not None


def test_setup_tracing_survives_missing_openai_instrumentor(monkeypatch):
    # Older bundles may lack the package; agents-level tracing must still install.
    import sys
    monkeypatch.delenv("NIMOOS_AGENT_TRACING", raising=False)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.openai", None)
    m = importlib.reload(pt)
    assert m.setup_tracing() is True
