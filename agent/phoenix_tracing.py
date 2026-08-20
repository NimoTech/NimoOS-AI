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
        # Also instrument the OpenAI client itself: the Agents SDK generation
        # span omits the request's tools list, so only client-level spans give
        # Phoenix the llm.tools.* attributes. Optional — bundles without the
        # package keep agents-only tracing.
        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor
            OpenAIInstrumentor().instrument(tracer_provider=provider)
        except Exception:
            _LOG.warning("OpenAI client instrumentation unavailable; "
                         "LLM spans will not carry tool definitions", exc_info=True)
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


import time as _time

_GLOBAL_SCOPE = "__global__"   # reserved user_id for process-wide settings;
                               # protect in any user-cleanup logic (whitelist).


def tracing_globally_enabled(conn) -> bool:
    try:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key='tracing_enabled'",
            (_GLOBAL_SCOPE,),
        ).fetchone()
    except Exception:
        return False
    return bool(row) and str(row["value"]) == "1"


def set_tracing_globally_enabled(conn, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES(?, 'tracing_enabled', ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (_GLOBAL_SCOPE, "1" if enabled else "0", int(_time.time())),
    )
    conn.commit()
    _set_flag(bool(enabled))


def refresh_enabled_flag(conn) -> None:
    _set_flag(tracing_globally_enabled(conn))


def _tool_error_formatter(args):
    """RunConfig.tool_error_formatter: rewrite "tool not found" for `mcp__*`
    names into instructions the model can act on. Returning None (any other
    error kind, any non-MCP tool, or any failure in here) keeps the SDK's own
    default message."""
    try:
        if getattr(args, "kind", "") != "tool_not_found":
            return None
        from skills import mcp_gating as _mcp
        return _mcp.tool_not_found_message(getattr(args, "tool_name", "") or "")
    except Exception:
        return None


def _tool_error_kwargs() -> dict:
    """Turn a call to a tool that isn't in the tool list from a run-killing
    ModelBehaviorError (the SDK default, run_config.py's
    tool_not_found_behavior="raise_error") into a tool result the model can
    recover from. Split out so an SDK version that lacks either parameter
    degrades to the old behaviour instead of failing every run — see
    build_trace_run_config's retry."""
    return {
        "tool_not_found_behavior": "return_error_to_model",
        "tool_error_formatter": _tool_error_formatter,
    }


def build_trace_run_config(enabled: bool, session_id, user_id, model_name, kind):
    """Always return a RunConfig. enabled=False → tracing_disabled=True.

    Also the single place this repo configures tool-error handling: there is
    exactly one RunConfig factory, and every run goes through it whether
    tracing is on or off.
    """
    from agents import RunConfig
    base = {"tracing_disabled": True} if not enabled else {
        "workflow_name": "nimoos-agent",
        "group_id": str(session_id),
        "trace_metadata": {
            "user_id": str(user_id),
            "model": str(model_name),
            "agent_type": str(kind),
        },
        "tracing_disabled": False,
    }
    try:
        return RunConfig(**base, **_tool_error_kwargs())
    except TypeError:
        # SDK too old for these parameters: keep the run working.
        _LOG.warning("SDK RunConfig rejected tool-error parameters; "
                     "falling back without them", exc_info=True)
    except Exception:
        _LOG.warning("build_trace_run_config failed; disabling trace for this run",
                     exc_info=True)
        return RunConfig(tracing_disabled=True)
    try:
        return RunConfig(**base)
    except Exception:
        _LOG.warning("build_trace_run_config failed; disabling trace for this run",
                     exc_info=True)
        return RunConfig(tracing_disabled=True)
