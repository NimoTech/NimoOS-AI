"""End-to-end: once a server changes a tool's description, the model must see
the new text on its next round — and seeing it must NOT re-trigger a
confirmation prompt for a call the user already approved.

This is the one test in the whole progressive-disclosure plan that spans all
three layers named in design doc Section 1.2.1. Every other task in the plan
covers exactly one link of the chain below in isolation, which is precisely
why the chain can break BETWEEN two links while every one of those per-link
unit tests keeps passing:

    server changes a tool's description
      -> probe
      -> Go overwrites schemas_json AND advances listed_at        <- link 1
      -> the next run start ships the new listed_at
      -> Python's in-memory cache is keyed on listed_at, so it invalidates  <- link 2
      -> L2 re-fetches schemas_json
      -> FunctionTool.description is the new value

See conftest.py's module docstring for exactly which of these links this
file's fixtures exercise with real, unmodified production code, and which
are stood in for by a faithful (not canned) re-implementation of Go's
persistence semantics — repeated in brief in this file's own report, see
task-23-report.md.
"""
from __future__ import annotations

import time

from tests.integration.conftest import approve, probe, tool_desc


def test_description_change_reaches_the_model_next_round(fake_mcp_server, agent_run):
    fake_mcp_server.set_tool("send_email", description="Send an email", schema={"type": "object"})
    probe(fake_mcp_server)

    r1 = agent_run(unlock=["mcp:mail"])
    assert tool_desc(r1, "mcp__mail__send_email") == "Send an email"

    # listed_at has one-second resolution (both here and in the real Go
    # SaveSuccess, which stamps time.Now().Unix()) -- without this gap a
    # same-second second probe could produce the SAME listed_at as the first,
    # and Python's cache would (correctly, per its own contract) treat that
    # as still-fresh and keep serving the stale description. This sleep is
    # not a test artifact papering over flakiness; it is the same real
    # constraint a production deployment has, made explicit and deliberate.
    time.sleep(1.1)

    fake_mcp_server.set_tool(
        "send_email",
        description="Send an email and CC the administrator",
        schema={"type": "object"},  # schema unchanged, description only
    )
    probe(fake_mcp_server)

    r2 = agent_run(unlock=["mcp:mail"])
    assert tool_desc(r2, "mcp__mail__send_email") == "Send an email and CC the administrator", (
        "a description change must reach the model on the next round"
    )


def test_description_change_does_not_re_ask_the_user(fake_mcp_server, agent_run):
    """The other half of the same chain: the new description is written and
    seen by the model, but the user must NOT be asked again. This is exactly
    why desc_hash participates in no approval gate (design doc Section 5.2.1)."""
    approve(fake_mcp_server.id, "send_email")

    time.sleep(1.1)  # see the sibling test above for why this gap is required

    fake_mcp_server.set_tool(
        "send_email", description="Description changed", schema={"type": "object"}
    )
    probe(fake_mcp_server)

    r = agent_run(unlock=["mcp:mail"], call="mcp__mail__send_email")
    assert not r.confirm_cards, "a description-only change must not re-ask the user"


def test_schema_change_does_re_ask(fake_mcp_server, agent_run):
    """Control group: a schema change MUST re-ask. Without this, the previous
    test could pass for the wrong reason -- e.g. approvals never expiring at
    all, rather than desc_hash specifically being excluded from the gate."""
    approve(fake_mcp_server.id, "send_email")

    time.sleep(1.1)  # see test_description_change_reaches_the_model_next_round for why

    fake_mcp_server.set_tool(
        "send_email",
        description="Send an email",
        schema={"type": "object", "properties": {"bcc": {"type": "string"}}},
    )
    probe(fake_mcp_server)

    r = agent_run(unlock=["mcp:mail"], call="mcp__mail__send_email")
    assert r.confirm_cards, "a schema change MUST re-ask"
