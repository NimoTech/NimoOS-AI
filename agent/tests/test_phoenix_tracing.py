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
    assert inner.calls == 0                      # disabled → 不发网络
    enabled["v"] = True
    assert gated.export(["span"]) == SpanExportResult.SUCCESS
    assert inner.calls == 1                       # enabled → 委托内层


def test_setup_tracing_opt_out(monkeypatch):
    monkeypatch.setenv("NIMOOS_AGENT_TRACING", "0")
    m = importlib.reload(pt)
    assert m.setup_tracing() is False             # 显式关 → 不安装


def test_setup_tracing_installs(monkeypatch):
    monkeypatch.delenv("NIMOOS_AGENT_TRACING", raising=False)
    m = importlib.reload(pt)
    assert m.setup_tracing() is True              # 依赖已装 → 安装成功
