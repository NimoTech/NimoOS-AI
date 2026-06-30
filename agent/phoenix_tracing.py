"""Optional Phoenix (OTLP) tracing for the agent.

Switch-gated via NIMOOS_AGENT_TRACING. When disabled (default) every entry
point is a no-op and no OTel/OpenInference dependency is imported. Any failure
during setup is swallowed — tracing must never break the agent.
"""
import logging
import os

_LOG = logging.getLogger("nimoos-agent.tracing")

_TRUTHY = {"1", "true", "yes", "on"}


def tracing_enabled() -> bool:
    return os.environ.get("NIMOOS_AGENT_TRACING", "").strip().lower() in _TRUTHY


def otlp_endpoint() -> str:
    return os.environ.get("PHOENIX_OTLP_ENDPOINT", "http://127.0.0.1:6006/v1/traces")


def project_name() -> str:
    return os.environ.get("PHOENIX_PROJECT", "nimoos-agent")


def _install_processors() -> None:
    """Replace the SDK's default OpenAI exporter with an OTLP→Phoenix one."""
    from agents import set_trace_processors
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
    from opentelemetry.sdk import trace as trace_sdk
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    # ★ Privacy: drop the default BackendSpanExporter so nothing goes to OpenAI.
    set_trace_processors([])

    provider = trace_sdk.TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint()))
    )
    OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)


def setup_tracing() -> bool:
    """Initialise tracing if enabled. Returns True on success, else False."""
    if not tracing_enabled():
        return False
    try:
        _install_processors()
        _LOG.info("agent tracing → Phoenix enabled (endpoint=%s, project=%s)",
                  otlp_endpoint(), project_name())
        return True
    except Exception:  # never break the agent on tracing failure
        _LOG.warning("agent tracing setup failed; continuing without tracing",
                     exc_info=True)
        return False


def build_trace_run_config(session_id, user_id, model_name, kind):
    """Return a RunConfig that groups this run by session, or None if disabled."""
    if not tracing_enabled():
        return None
    try:
        from agents import RunConfig
        return RunConfig(
            workflow_name="nimoos-agent",
            group_id=str(session_id),
            trace_metadata={
                "user_id": str(user_id),
                "model": str(model_name),
                "agent_type": str(kind),
            },
        )
    except Exception:
        _LOG.warning("build_trace_run_config failed; running without trace metadata",
                     exc_info=True)
        return None
