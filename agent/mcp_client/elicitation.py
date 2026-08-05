"""The elicitation callback: a server's mid-call question -> a card the user answers
-> an ElicitResult back on the wire.

Two rules that are easy to break by accident, both load-bearing:

  1. NEVER return ErrorData to refuse. `_dispatch_all`
     (mcp/client/_input_required.py:99-127) calls this callback CONCURRENTLY for every
     key in one round, and the first task to return ErrorData calls
     `tg.cancel_scope.cancel()` on its siblings. Refusing that way would throw away
     answers the user had already typed into the OTHER cards of the same round.
     Refusal is `ElicitResult(action="decline")`.

  2. The user's ANSWER never touches disk. We persist the QUESTION — so a card
     survives a reconnect, which the spec's "provide manual controls that let the user
     retry or cancel" SHOULD effectively requires — and nothing else. Not in
     event_log, not in pending_confirmations, not in the audit trail. The spec forbids
     servers from using form mode for passwords / API keys / tokens; a non-compliant
     server is precisely the threat.

The run-scoped ContextVars are passed IN rather than imported from mcp_client.client:
that keeps this module free of a circular import, and it lets the tests hand in their
own vars instead of mutating module state.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from mcp.types import ElicitRequestURLParams, ElicitResult

from mcp_client.elicitation_schema import render_fields, validate_content


# How many times we re-ask when the answer fails validation, before giving up.
#
# Why re-ask at all: validate_content is the only real gate (the SDK validates
# requestedSchema responses not at all), and a one-shot decline BURNS the user's
# answer — the confirm_id is consumed, the card is resolved, and there is no path back
# to "fill it in again". They would have to ask the agent to redo the whole tool call.
#
# Why re-asking is free: _dispatch_all awaits this callback with NO timeout around it,
# and the MRTR round counter does not advance while we are awaiting (rounds only tick
# on retry). The 60s read_timeout_seconds is per round trip and applies between rounds,
# not during one. So an extra question costs nothing protocol-side.
#
# Why bounded: a schema constraint we render but cannot satisfy (or a card bug) would
# otherwise loop forever against a user who keeps trying.
#
# The re-asked card comes back EMPTY, carrying only the reason — the previous answer is
# deliberately not echoed back, because the card event goes through RunSink.put into
# event_log and that would put the answer on disk. Re-typing beats persisting a secret.
MAX_ANSWER_ATTEMPTS = 3


def _has_punycode(host: str) -> bool:
    """True when any label is an `xn--` IDN in ASCII form.

    We do not block these — plenty are legitimate. We make the card SAY so, because an
    IDN label can render as a homograph of a brand domain, and the one thing the user
    is being asked to judge here is "do I trust this site with my account".
    """
    return any(label.startswith("xn--") for label in (host or "").lower().split("."))


def _url_card(server: dict, params) -> tuple[dict, str]:
    try:
        host = urlsplit(params.url).hostname or ""
    except Exception:
        host = ""
    card = {"type": "confirmation_required", "kind": "mcp_elicit_url",
            "server": server.get("name", "mcp"),
            "message": params.message,
            "url": params.url,
            "host": host,
            "punycode": _has_punycode(host),
            "insecure": not str(params.url).lower().startswith("https://")}
    return card, f"{params.message} [{params.url}]"


def _form_card(server: dict, params) -> tuple[dict, str]:
    card = {"type": "confirmation_required", "kind": "mcp_elicit_form",
            "server": server.get("name", "mcp"),
            "message": params.message,
            "fields": render_fields(params.requested_schema)}
    return card, params.message


def make_elicitation_callback(server: dict, *, session_id_var, queue_var, mgr_var):
    """Build the `elicitation_callback` for one server's Client.

    Passing this to `Client(...)` declares BOTH elicitation modes — see the comment at
    the call site in mcp_client/client.py::_connect.
    """

    async def _elicit(context, params):
        mgr = mgr_var.get()
        queue = queue_var.get()
        session_id = session_id_var.get()
        if mgr is None or queue is None or not session_id:
            # No run context: this Client belongs to a schema prefetch (_cold_fetch /
            # _revalidate) or to the /test probe. There is no browser to ask, and
            # blocking here would turn a background refresh into a task that never
            # returns. Decline is the honest answer, and it is a normal answer — not
            # an error (see rule 1).
            return ElicitResult(action="decline")

        is_url = isinstance(params, ElicitRequestURLParams)
        card, question = (_url_card(server, params) if is_url
                          else _form_card(server, params))

        reason = None
        for _ in range(MAX_ANSWER_ATTEMPTS):
            confirm_id = mgr.register(
                session_id,
                f"mcp_elicit:{server.get('id')}",
                f'MCP server "{server.get("name", "mcp")}" is asking for input',
                question)      # the QUESTION only — never the answer (rule 2)
            card = dict(card, confirm_id=confirm_id, error=reason)
            await queue.put(card)

            action, content = await mgr.wait_elicit(confirm_id)
            if action != "accept":
                return ElicitResult(action=action)

            if is_url:
                # Per spec, "accept" on a URL elicitation means only that the user
                # consented to open the link — explicitly NOT that the interaction
                # completed. There is nothing to carry back or validate, and the server
                # is expected to keep saying "not ready" until the out-of-band
                # authorization lands. Phase 2 accepts that this usually ends in
                # InputRequiredRoundsExceededError; mcp_client/client.py::
                # _rounds_exceeded_msg is what makes that legible.
                return ElicitResult(action="accept")

            reason = validate_content(params.requested_schema, content or {})
            if reason is None:
                return ElicitResult(action="accept", content=content or {})
            # Invalid — ask again with the reason attached rather than burning the
            # user's answer on a decline they cannot recover from. See the re-ask
            # comment above MAX_ANSWER_ATTEMPTS for why this is free.

        # Out of attempts: something neither side can satisfy (a schema constraint we
        # render but cannot express, say). Give up cleanly instead of looping forever.
        await queue.put({"type": "mcp_warning", "server": server.get("name", "mcp"),
                         "error": f"could not get a valid answer: {reason}"})
        return ElicitResult(action="decline")

    return _elicit
