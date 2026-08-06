"""Render an MCP elicitation `requestedSchema` into flat field descriptors, and
validate the user's answer back against it.

Why this file exists: mcp 2.0 types `ElicitRequestFormParams.requested_schema` as a
bare `dict[str, Any]` — `types.ElicitRequestedSchema` IS that alias, and
`types.RequestedSchema` (which the phase-2 handoff named) does not exist. The SDK
therefore does zero modelling and zero validation of it, while the spec says
"Clients SHOULD validate all responses against the provided schema". All of it is
ours.

Scope is deliberately NOT a general JSON Schema form generator. `ElicitResult.content`
is pinned by the SDK to flat scalars — `dict[str, str | int | float | bool | list[str]
| None]` — and pydantic rejects nested objects, object arrays, and even `list[int]`
outright. So the reachable surface is: text, number, boolean, single choice, multi
choice. That is exactly what the spec's elicitation subset describes.

No SDK import here on purpose: this module is pure data-in/data-out so it can be unit
tested without a session, and so a schema quirk can never take a connection down.
"""
from __future__ import annotations

import re
from typing import Any

# The four the spec names. An unrecognised `format` is dropped rather than passed
# through — advertising a constraint we do not actually check would be worse than
# admitting we treat the field as free text.
_FORMATS = ("email", "uri", "date", "date-time")

# VERBATIM the WHATWG HTML5 "valid e-mail address" production, because the card
# renders these fields as <input type="email"> and the browser enforces exactly this
# regex. Writing our own stricter one (e.g. requiring a dot in the domain) would make
# the browser accept `a@b` and this function then reject it — a rejection the user
# cannot see coming and, before the re-ask loop existed, could not recover from. The
# two sides agree here BY CONSTRUCTION, not by anyone remembering to keep them in sync.
# Source: https://html.spec.whatwg.org/multipage/input.html#valid-e-mail-address
# Pinned by test_email_rule_is_the_whatwg_one_the_browser_enforces.
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$")

# `uri` has NO native input type behind it: <input type="url"> is STRICTER than this
# and would reject values we accept, leaving the user unable to submit at all. So uri
# fields render as plain text and this is their only gate — a rejection here comes back
# through the re-ask loop with the reason attached.
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

# These two match what <input type="date"> and <input type="datetime-local"> actually
# produce (`YYYY-MM-DD` and `YYYY-MM-DDTHH:MM`), so a value the control emits always
# passes. Pinned by test_date_rules_accept_what_the_native_controls_emit.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}")

_FORMAT_CHECKS = {
    "email": lambda v: bool(_EMAIL_RE.match(v)),
    "uri": lambda v: bool(_URI_RE.match(v)),
    "date": lambda v: bool(_DATE_RE.match(v)),
    "date-time": lambda v: bool(_DATETIME_RE.match(v)),
}


def _int_or_none(v: Any) -> int | None:
    # bool is an int subclass in Python; `minLength: true` must not become 1.
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _num_or_none(v: Any) -> float | int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _is_scalar(v: Any) -> bool:
    """True for the value types `ElicitResult.content` can actually carry.

    Why this gate exists, and why it DROPS rather than stringifies: a server-supplied
    `const` of `{"a": 1}` used to be copied through verbatim, and every downstream check
    waved it past — `_validate_one`'s enum rule is pure membership, so a value that came
    OUT of the schema always passes going back IN. The first thing that noticed was
    pydantic, at `ElicitResult(action="accept", content=...)`, i.e. INSIDE the
    elicitation callback, where a raise becomes an ExceptionGroup out of `_dispatch_all`
    and cancels every sibling card in the round.

    Stringifying (what the array branch does) would be the other option, but it is only
    right there: arrays are pinned to `list[str]` by the model, so a non-string element
    genuinely cannot exist. Scalar fields have no such pin — `{"const": 1}` round-trips
    as the integer 1 today, and stringifying would silently change the value the server
    receives. Dropping loses nothing that was ever selectable.

    bool is deliberately included: `ElicitResult.content` accepts it.
    """
    return isinstance(v, (str, int, float, bool))


def _options(node: dict) -> list[dict] | None:
    """Pull a choice list out of `node`, or None if it isn't a choice at all.

    Three shapes the spec allows, in precedence order:
      oneOf/anyOf: [{const, title}, ...]   titled choices
      enum: [...] (+ optional enumNames)   bare choices
    A oneOf/anyOf branch missing `const` is NOT the elicitation choice shape (it is
    some richer JSON Schema construct we do not model), so we bail to None and the
    caller degrades the field to free text rather than silently dropping branches.

    A branch whose `const` is not a scalar IS dropped (see `_is_scalar`), and when that
    leaves nothing we return None so the caller degrades the field to free text —
    never nothing. An unanswerable card is worse than a loosely-typed one.
    """
    for kw in ("oneOf", "anyOf"):
        branch = node.get(kw)
        if isinstance(branch, list) and branch:
            out = []
            for b in branch:
                if not isinstance(b, dict) or "const" not in b:
                    return None
                if not _is_scalar(b["const"]):
                    continue
                out.append({"value": b["const"],
                            "title": str(b.get("title") or b["const"])})
            return out or None
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        names = node.get("enumNames")
        names = names if isinstance(names, list) and len(names) == len(enum) else None
        out = [{"value": v, "title": str(names[i]) if names else str(v)}
               for i, v in enumerate(enum) if _is_scalar(v)]
        return out or None
    return None


def _blank(key: str, prop: dict, required: bool) -> dict:
    return {"key": key,
            "type": "string",
            "title": str(prop.get("title") or key),
            "description": str(prop.get("description") or ""),
            "required": required,
            "default": prop.get("default"),
            "format": None, "min_length": None, "max_length": None,
            "minimum": None, "maximum": None,
            "options": None, "min_items": None, "max_items": None}


def _render_one(key: str, prop: dict, required: bool) -> dict:
    f = _blank(key, prop, required)

    opts = _options(prop)
    if opts is not None:
        f["type"] = "enum"
        f["options"] = opts
        return f

    jtype = prop.get("type")

    if jtype == "array":
        items = prop.get("items")
        items = items if isinstance(items, dict) else {}
        f["type"] = "multi_enum"
        # Stringified on purpose: ElicitResult.content types arrays as list[str] and
        # pydantic rejects list[int]. A non-string const simply cannot survive the
        # round trip, so we normalise here and compare as strings in validation.
        f["options"] = [{"value": str(o["value"]), "title": o["title"]}
                        for o in (_options(items) or [])]
        f["min_items"] = _int_or_none(prop.get("minItems"))
        f["max_items"] = _int_or_none(prop.get("maxItems"))
        return f

    if jtype == "boolean":
        f["type"] = "boolean"
        return f

    if jtype in ("integer", "number"):
        f["type"] = jtype
        f["minimum"] = _num_or_none(prop.get("minimum"))
        f["maximum"] = _num_or_none(prop.get("maximum"))
        return f

    # "string", plus the deliberate catch-all. An unrecognised type degrades to a free
    # text box instead of vanishing: a dropped required field is an unanswerable card.
    if jtype == "string" or jtype is None or True:
        fmt = prop.get("format")
        f["format"] = fmt if fmt in _FORMATS else None
        f["min_length"] = _int_or_none(prop.get("minLength"))
        f["max_length"] = _int_or_none(prop.get("maxLength"))
    return f


def render_fields(requested_schema: Any) -> list[dict]:
    """`requestedSchema` -> ordered field descriptors (see the plan for the contract).

    Never raises: a malformed schema from a remote server must degrade to a card the
    user can still look at, not take down the tool call.
    """
    if not isinstance(requested_schema, dict):
        return []
    props = requested_schema.get("properties")
    if not isinstance(props, dict):
        return []
    raw_required = requested_schema.get("required")
    required = set(raw_required) if isinstance(raw_required, list) else set()
    return [_render_one(key, prop if isinstance(prop, dict) else {}, key in required)
            for key, prop in props.items()]


def _range(f: dict, v) -> str | None:
    if f["minimum"] is not None and v < f["minimum"]:
        return f"must be at least {f['minimum']}"
    if f["maximum"] is not None and v > f["maximum"]:
        return f"must be at most {f['maximum']}"
    return None


def _validate_one(f: dict, v) -> str | None:
    t = f["type"]

    if t == "boolean":
        return None if isinstance(v, bool) else "must be true or false"

    if t == "integer":
        # isinstance(True, int) is True — without this guard a checkbox answer would
        # sail through as age=1.
        if isinstance(v, bool) or not isinstance(v, int):
            return "must be a whole number"
        return _range(f, v)

    if t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "must be a number"
        return _range(f, v)

    if t == "enum":
        allowed = [o["value"] for o in (f["options"] or [])]
        return None if v in allowed else "is not one of the offered choices"

    if t == "multi_enum":
        if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
            return "must be a list of strings"
        allowed = {o["value"] for o in (f["options"] or [])}
        if allowed and any(x not in allowed for x in v):
            return "contains a value that is not one of the offered choices"
        if f["min_items"] is not None and len(v) < f["min_items"]:
            return f"pick at least {f['min_items']}"
        if f["max_items"] is not None and len(v) > f["max_items"]:
            return f"pick at most {f['max_items']}"
        return None

    if not isinstance(v, str):
        return "must be text"
    if f["min_length"] is not None and len(v) < f["min_length"]:
        return f"must be at least {f['min_length']} characters"
    if f["max_length"] is not None and len(v) > f["max_length"]:
        return f"must be at most {f['max_length']} characters"
    fmt = f["format"]
    if fmt and not _FORMAT_CHECKS[fmt](v):
        return f"is not a valid {fmt}"
    return None


def validate_content(requested_schema: Any, content: Any) -> str | None:
    """None when `content` is a legal answer, else one short human-readable reason.

    Authoritative — the SDK validates `requestedSchema` responses not at all, and the
    only backstop past this point is `ElicitResult`'s own pydantic model, which checks
    coarse value types and nothing about the schema.
    """
    if not isinstance(content, dict):
        return "the answer must be an object"
    fields = {f["key"]: f for f in render_fields(requested_schema)}
    unknown = sorted(set(content) - set(fields))
    if unknown:
        return f"unknown field(s): {', '.join(unknown)}"
    for key, f in fields.items():
        if content.get(key) is None:
            if f["required"]:
                return f"{f['title']}: is required"
            continue
        err = _validate_one(f, content[key])
        if err:
            return f"{f['title']}: {err}"
    return None
