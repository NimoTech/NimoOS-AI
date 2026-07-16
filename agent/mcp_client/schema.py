"""Adapt MCP tool inputSchema to what openai-agents FunctionTool accepts, and
flatten MCP CallToolResult into the plain text the SDK feeds back to the LLM."""
from __future__ import annotations

from typing import Any

from fences import fence_untrusted


def sanitize_schema(input_schema: Any) -> dict:
    """Return a JSON-Schema object safe to hand to FunctionTool as
    params_json_schema. MCP schemas are NOT OpenAI strict-mode compatible, so the
    tool is registered with strict_json_schema=False (see client.py); here we
    only guarantee a well-formed object schema."""
    if not isinstance(input_schema, dict):
        return {"type": "object", "properties": {}}
    schema = dict(input_schema)
    if schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    schema.setdefault("properties", {})
    return schema


def flatten_result(result: Any) -> str:
    """Best-effort: concatenate text blocks of an MCP CallToolResult. Unknown
    block types are stringified. Marks errors so the LLM can react."""
    content = getattr(result, "content", None)
    parts: list[str] = []
    if content:
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(str(block))
    body = "\n".join(parts)
    if getattr(result, "isError", False):
        body = f"[tool error] {body}".strip()
    # Third-party MCP server output is untrusted external content — fence it as
    # data so an injected instruction in a tool result can't drive the agent.
    return fence_untrusted("mcp-result", body, cap=60000) or body
