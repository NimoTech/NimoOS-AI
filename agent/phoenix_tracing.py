"""Optional Phoenix (OTLP) tracing for the agent.

Instrumentation is installed once at startup (if deps import). Whether spans are
actually exported is gated at runtime by an in-process flag (_enabled), synced
from the global user_settings row and updated by the settings endpoint — so the
UI toggle takes effect on the next run with no restart. When disabled, the
GatedSpanExporter drops without touching the network, so stopping Phoenix never
causes OTLP connection-retry log spam. Any setup failure is swallowed.
"""
import logging
import os

_LOG = logging.getLogger("nimoos-agent.tracing")

# In-process enable flag; synced from user_settings at boot and on PUT.
_enabled = False


def tracing_enabled_now() -> bool:
    return _enabled


def _set_flag(v: bool) -> None:
    global _enabled
    _enabled = bool(v)


def otlp_endpoint() -> str:
    return os.environ.get("PHOENIX_OTLP_ENDPOINT", "http://127.0.0.1:6006/v1/traces")


def project_name() -> str:
    return os.environ.get("PHOENIX_PROJECT", "nimoos-agent")


try:
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class GatedSpanExporter(SpanExporter):
        """Wrap an OTLP exporter; drop (no network) unless is_enabled()."""
        def __init__(self, inner, is_enabled):
            self._inner = inner
            self._is_enabled = is_enabled

        def export(self, spans):
            if not self._is_enabled():
                return SpanExportResult.SUCCESS
            return self._inner.export(spans)

        def shutdown(self):
            return self._inner.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return self._inner.force_flush(timeout_millis)
except Exception:  # opentelemetry not installed — GatedSpanExporter unused
    GatedSpanExporter = None  # type: ignore


def _opted_out() -> bool:
    return os.environ.get("NIMOOS_AGENT_TRACING", "").strip().lower() in {"0", "off", "false", "no"}


def setup_tracing() -> bool:
    """Install instrumentation + gated OTLP exporter. Returns True on success."""
    if _opted_out():
        return False
    try:
        from agents import set_trace_processors
        from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
        from opentelemetry.sdk import trace as trace_sdk
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        set_trace_processors([])  # drop SDK's default OpenAI exporter
        resource = Resource.create({"openinference.project.name": project_name()})
        provider = trace_sdk.TracerProvider(resource=resource)
        gated = GatedSpanExporter(OTLPSpanExporter(endpoint=otlp_endpoint()),
                                  tracing_enabled_now)
        provider.add_span_processor(BatchSpanProcessor(gated))
        OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)
        # Suppress transient OTLP export errors (enabled but Phoenix briefly down).
        logging.getLogger("opentelemetry.exporter.otlp").setLevel(logging.CRITICAL)
        logging.getLogger("opentelemetry.sdk.trace.export").setLevel(logging.CRITICAL)
        _LOG.info("agent tracing installed (endpoint=%s, project=%s)",
                  otlp_endpoint(), project_name())
        return True
    except Exception:
        _LOG.warning("agent tracing setup failed; continuing without tracing",
                     exc_info=True)
        return False
