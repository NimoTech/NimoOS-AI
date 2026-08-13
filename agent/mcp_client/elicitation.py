"""The elicitation callback: a server's mid-call question -> a card the user answers
-> an ElicitResult back on the wire.

Two rules that are easy to break by accident, both load-bearing:

  1. NEVER return ErrorData to refuse. `_dispatch_all`
     (mcp/client/_input_required.py:99-127) calls this callback CONCURRENTLY for every
     key in one round, and the first task to return ErrorData calls
     `tg.cancel_scope.cancel()` on its siblings. Refusing that way would throw away
     answers the user had already typed into the OTHER cards of the same round.
     Refusal is `ElicitResult(action="decline")`. A RAISE is just as fatal — it comes
     out of the task group as an ExceptionGroup with the same blast radius — so
     `_elicit` wraps its whole body in a catch-all that degrades to decline. An
     absolute invariant needs a structural guarantee, not a docstring; pinned by
     test_a_failure_in_one_card_does_not_destroy_a_siblings_answer.

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

import asyncio
import logging
from urllib.parse import urlsplit

from mcp.types import ElicitRequestURLParams, ElicitResult

from mcp_client.elicitation_schema import render_fields, validate_content

logger = logging.getLogger(__name__)


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


# How long a URL authorization card waits before we send `accept` anyway.
#
# The whole point of the URL card is that the WAIT happens here rather than in the
# protocol loop: `_dispatch_all` awaits this callback with no deadline, the MRTR round
# counter only ticks on `retry`, and while we are awaiting there is no in-flight
# request at all (the server already answered with a complete InputRequiredResult), so
# the 60s per-round read timeout is not running either. The only clock is this one.
#
# Why not the 24h DEFAULT_TIMEOUT the form card uses: the card holds the server's
# `requestState`, and the spec advises servers to give that a SHORT TTL and validate it
# on arrival. An `accept` sent a day later lands on expired state — strictly worse than
# one sent in three minutes.
#
# Why not shorter: login + MFA + a consent screen on a slow phone is minutes, not
# seconds. Three is comfortably past the realistic median and still well inside our
# own 900s sse idle read timeout (see client.py::MCP_SSE_READ_TIMEOUT for why that
# matters — that value is our own explicit override, not the SDK's 300s default).
#
# What happens at the deadline is `on_timeout="accept"`, not "cancel" — see the
# wait_elicit call below.
URL_ELICIT_WAIT = 180


# The only two schemes a URL elicitation may carry.
#
# `window.open(url)` in the card navigates a real browser to a fully SERVER-CONTROLLED
# string. `javascript:` executes in a document that inherits the opener's origin in
# several browsers; `data:` and `blob:` render attacker HTML that the user reads as
# "a page NimoOS opened for me"; a registered custom protocol handler launches a native
# app. None of that is "authorize on an external site", which is the entire meaning of
# this elicitation mode. The card's HTTPS notice is a WARNING, not a gate, so the gate
# has to be here — and mirrored in the card, because the two ship from independent
# repos and either one can be an older build.
_ALLOWED_URL_SCHEMES = ("http", "https")


def _split(url):
    """`urlsplit` that never raises — a malformed URL must degrade, not kill the call.

    (urlsplit itself raises ValueError on e.g. an unterminated IPv6 literal.)
    """
    try:
        return urlsplit(str(url))
    except Exception:
        return urlsplit("")


def _host_flags(host: str) -> tuple[bool, str]:
    """(punycode, ascii_form) for the host we are about to ask the user to judge.

    `punycode` is True when the host is an IDN in EITHER form: an ASCII `xn--` label, or
    non-ASCII characters that IDNA would encode into one. Both carry the same risk — a
    label that renders as a homograph of a brand domain — and the second is the one that
    actually bites: `urlsplit().hostname` does NOT idna-encode, so a Cyrillic
    "аpple.com" arrives verbatim and the card renders it in bold highlight,
    indistinguishable from the real thing. Matching only "xn--" warned about the
    visibly-suspicious spelling and stayed silent on the invisible one, which inverts
    the feature.

    `ascii_form` is the punycode spelling, and is non-empty ONLY when it differs from
    what the card renders — i.e. exactly when the user cannot tell by looking. The card
    shows it next to the pretty form.

    We still do not BLOCK IDNs: plenty are legitimate. We make the card say so.
    """
    host = host or ""
    if not host:
        return False, ""
    if host.isascii():
        return any(lbl.startswith("xn--") for lbl in host.lower().split(".")), ""
    try:
        ascii_form = host.encode("idna").decode("ascii")
    except Exception:
        # Not IDNA-encodable (empty/overlong label, disallowed codepoint). Still
        # non-ASCII, so still worth warning about; we just cannot show the ASCII form.
        return True, ""
    return True, ascii_form if ascii_form.lower() != host.lower() else ""


def _url_card(server: dict, params) -> tuple[dict, str]:
    parts = _split(params.url)
    host = parts.hostname or ""
    punycode, host_ascii = _host_flags(host)
    card = {"type": "confirmation_required", "kind": "mcp_elicit_url",
            "server": server.get("name", "mcp"),
            "message": params.message,
            "url": params.url,
            "host": host,
            "punycode": punycode,
            "host_ascii": host_ascii,
            # Parsed, not string-prefixed: `"  https://ok.example/x".startswith(...)`
            # is False and used to flag a perfectly good HTTPS URL as insecure, while
            # urlsplit tolerates the same leading whitespace the browser does.
            "insecure": parts.scheme.lower() != "https"}
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
        # Rule 1 is stated as an ABSOLUTE, and a docstring cannot hold an absolute — the
        # body below reaches SQLite (mgr.register / wait_elicit's _cleanup both
        # execute+commit with no handler of their own, so a locked or full database
        # raises here) and pydantic (ElicitResult construction). Any of those escaping
        # becomes an ExceptionGroup out of _dispatch_all, which cancels every SIBLING
        # card of the same round and destroys answers the user already typed. So the
        # invariant gets a structural guarantee, not a promise.
        #
        # CancelledError is re-raised on purpose: that is the task group legitimately
        # tearing us down, and swallowing it would break cancellation.
        try:
            return await _elicit_inner(context, params)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never silent: this path means a bug or a broken database, and a card that
            # just quietly declines with no trace is the worst possible failure mode.
            logger.warning("elicitation callback for server %r failed; declining",
                           server.get("name", "mcp"), exc_info=True)
            return ElicitResult(action="decline")

    async def _elicit_inner(context, params):
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

        if is_url:
            scheme = _split(params.url).scheme.lower()
            if scheme not in _ALLOWED_URL_SCHEMES:
                # Do not build the card at all — a card is an invitation to click, and
                # there is no answer to this question we would be willing to act on.
                # Note the ordering: this sits AFTER the no-run-context guard above, so
                # `queue` is never None here.
                await queue.put({
                    "type": "mcp_warning", "server": server.get("name", "mcp"),
                    "error": f"refused an authorization link with an unsupported "
                             f"scheme ({scheme or 'none'}): only http and https can be "
                             f"opened"})
                return ElicitResult(action="decline")

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

            if is_url:
                # A URL card never re-asks (there is nothing to validate), so this
                # branch always returns on the first pass through the loop. It waits
                # DIFFERENTLY from a form card, which is why it has its own
                # wait_elicit call instead of sharing the one below:
                #
                #   - URL_ELICIT_WAIT, not the manager's 24h: the server's
                #     `requestState` has a short TTL. See the constant's comment.
                #   - on_timeout="accept", not the default "cancel": the user
                #     consented to open the page, and per spec `accept` asserts ONLY
                #     that consent — *"The response with action: 'accept' indicates
                #     that the user has consented to the interaction. It does not mean
                #     that the interaction is complete."* So it is still true after a
                #     timeout, and sending it is what gives a long-polling or
                #     state-only server the chance to return a terminal result.
                #     Sending "cancel" instead would kill a call whose authorization
                #     may well have succeeded in the browser.
                #
                # A user who explicitly cancels still gets "cancel" — wait_elicit only
                # applies on_timeout on a real timeout.
                action, _ = await mgr.wait_elicit(
                    confirm_id, timeout=URL_ELICIT_WAIT, on_timeout="accept")
                return ElicitResult(action=action)

            action, content = await mgr.wait_elicit(confirm_id)
            if action != "accept":
                return ElicitResult(action=action)

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
