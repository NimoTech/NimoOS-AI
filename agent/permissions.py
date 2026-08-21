"""Global permission policy — which confirmation gates auto-approve.

One box-wide document in `user_settings` under the reserved `__global__`
user_id (same precedent as web/settings.py), admin-scoped at the gateway.
Every interactive gate asks ONE question before raising a card:
`auto_approve(conn, action, level=...)`. The answer folds together the gate's
own mode and the run context (interactive / scheduled task / channel).

Fail-safe direction: any error — unreadable row, junk JSON, unknown value —
resolves to "ask", never to "auto". A policy read must never open a gate by
accident; the worst failure mode is the pre-existing behavior (a card).

Deliberately NOT configurable here (the hard floor, see the 2026-08-21 spec):
fs hard blacklists, FS_DENY_ROOTS, egress `block` verdicts, cloud-metadata
IPs, port policy, `/` grants, `protected` shell, MCP elicitation.
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar

_GLOBAL_SCOPE = "__global__"
_KEY = "permission_policy"

# Who is driving this run. Set by agent.py at run start from _start_run's
# `run_context` ("interactive" web/Photos chat, "task" scheduled run,
# "channel" TG/Discord/Lark). Read-only everywhere else. The default covers
# every code path that never set it — including main.py's HTTP handlers
# (the proxy's egress-confirm callback runs outside any agent run).
RUN_CONTEXT_VAR: ContextVar[str] = ContextVar("run_context", default="interactive")

GATE_MODES = ("ask", "auto")
SHELL_MODES = ("ask", "auto_gray", "auto_all")
CONTEXT_MODES = ("strict", "follow", "auto")

_GATES_DEFAULT = {
    "apps": "ask",
    "message_bus": "ask",
    "notes": "ask",
    "wiki": "ask",
    "installs": "ask",
    "fs_access": "ask",
    "mcp_tools": "ask",
    "network": "ask",
    "upload": "ask",
    "shell": "ask",
}
_JUDGES_DEFAULT = {"shell": True, "egress": True}
_CONTEXTS_DEFAULT = {"tasks": "strict", "channels": "strict"}
# Mirrors the egress-proxy's own flag defaults (-tofu-ttl 1h,
# -upload-threshold 65536). Applied at proxy spawn; a change needs a service
# restart, which the UI says out loud.
_PROXY_DEFAULT = {"tofu_ttl_hours": 1, "upload_threshold_kb": 64}

TOFU_TTL_HOURS_MAX = 24 * 30          # a month; 0 is invalid (proxy needs >0)
UPLOAD_THRESHOLD_KB_MAX = 1024 * 1024  # 1 GiB expressed in KiB

_ACTION_GATE = {
    "install_app": "apps", "start_app": "apps", "stop_app": "apps",
    "restart_app": "apps", "uninstall_app": "apps", "update_app": "apps",
    "trigger_action": "message_bus",
    "notes_write": "notes", "notes_update": "notes",
    "wiki_append_notes": "wiki", "wiki_replace_notes": "wiki",
    "wiki_register_root": "wiki",
    "grant_access": "fs_access",
    "shell_command": "shell",
    # bwrap-legacy "may this command use the network" — the same question the
    # netns proxy's TOFU gate asks, so it follows the network gate, not shell.
    "shell_network": "network",
    "egress_upload": "upload",
    # The proxy callback's TOFU reason. Its upload_over_threshold sibling is
    # mapped by the caller (main.py) to "upload" explicitly — the stored
    # `action` string is "egress" for both, so reason disambiguates there.
    "egress": "network",
}
_PREFIX_GATE = (
    ("mcp_install:", "installs"),
    ("toolbox_install:", "installs"),
    ("mcp_call:", "mcp_tools"),
)


def gate_of(action: str) -> str | None:
    """Canonical gate key for a ConfirmManager action string, or None for
    actions that must never auto-approve (elicitation, unknown)."""
    if not isinstance(action, str):
        return None
    g = _ACTION_GATE.get(action)
    if g:
        return g
    for prefix, gate in _PREFIX_GATE:
        if action.startswith(prefix):
            return gate
    return None


def default_policy() -> dict:
    return {
        "preset": "custom",
        "gates": dict(_GATES_DEFAULT),
        "judges": dict(_JUDGES_DEFAULT),
        "contexts": dict(_CONTEXTS_DEFAULT),
        "proxy": dict(_PROXY_DEFAULT),
    }


def _merge(doc: dict) -> dict:
    """Overlay a stored document onto the defaults, dropping unknown keys and
    coercing every invalid value back to its default (fail toward asking)."""
    out = default_policy()
    if not isinstance(doc, dict):
        return out
    preset = doc.get("preset")
    if isinstance(preset, str) and preset in ("custom", "strict", "balanced", "trusted"):
        out["preset"] = preset
    gates = doc.get("gates")
    if isinstance(gates, dict):
        for k in _GATES_DEFAULT:
            v = gates.get(k)
            valid = SHELL_MODES if k == "shell" else GATE_MODES
            if isinstance(v, str) and v in valid:
                out["gates"][k] = v
    judges = doc.get("judges")
    if isinstance(judges, dict):
        for k in _JUDGES_DEFAULT:
            if isinstance(judges.get(k), bool):
                out["judges"][k] = judges[k]
    contexts = doc.get("contexts")
    if isinstance(contexts, dict):
        for k in _CONTEXTS_DEFAULT:
            v = contexts.get(k)
            if isinstance(v, str) and v in CONTEXT_MODES:
                out["contexts"][k] = v
    proxy = doc.get("proxy")
    if isinstance(proxy, dict):
        ttl = proxy.get("tofu_ttl_hours")
        if isinstance(ttl, int) and not isinstance(ttl, bool) \
                and 1 <= ttl <= TOFU_TTL_HOURS_MAX:
            out["proxy"]["tofu_ttl_hours"] = ttl
        thr = proxy.get("upload_threshold_kb")
        if isinstance(thr, int) and not isinstance(thr, bool) \
                and 1 <= thr <= UPLOAD_THRESHOLD_KB_MAX:
            out["proxy"]["upload_threshold_kb"] = thr
    return out


def load(conn) -> dict:
    """The effective policy (defaults merged). Never raises."""
    try:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key=?",
            (_GLOBAL_SCOPE, _KEY),
        ).fetchone()
    except Exception:  # noqa: BLE001 — a broken read must degrade to defaults
        return default_policy()
    if not row:
        return default_policy()
    try:
        return _merge(json.loads(row["value"]))
    except (ValueError, TypeError):
        return default_policy()


def save(conn, doc: dict) -> dict:
    """Normalize + persist. Returns the normalized document (what load() will
    now say), so the API can echo the truth rather than the request."""
    normalized = _merge(doc)
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES(?, ?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (_GLOBAL_SCOPE, _KEY, json.dumps(normalized, ensure_ascii=False),
         int(time.time())),
    )
    conn.commit()
    return normalized


def _shell_auto(mode: str, level: str) -> bool:
    """Does shell mode `mode` cover a command classified at `level`?
    `protected` is never covered — in any mode, in any context."""
    if mode == "auto_gray":
        return level == "gray"
    if mode == "auto_all":
        return level in ("gray", "dangerous")
    return False


def auto_approve(conn, action: str, *, level: str = "",
                 context: str | None = None) -> bool:
    """Should the gate for `action` skip its card and proceed?

    `level` is the shell_guard classification, meaningful only for shell
    actions. `context` overrides RUN_CONTEXT_VAR for callers that answer on
    behalf of a run they do not execute inside (TaskRunDriver, channel
    router). Never raises: any failure means False (ask).
    """
    try:
        gate = gate_of(action)
        if gate is None:
            return False
        ctx = context if context is not None else RUN_CONTEXT_VAR.get()
        policy = load(conn)
        gates = policy["gates"]
        if ctx in ("task", "channel"):
            cmode = policy["contexts"]["tasks" if ctx == "task" else "channels"]
            if cmode == "strict":
                return False
            if cmode == "auto":
                # Unattended/remote contexts cap shell at gray in EVERY mode:
                # nobody is watching a scheduled run, and a channel user gets
                # a button for anything above gray instead of silence.
                if gate == "shell":
                    return level == "gray"
                return True
            # "follow" falls through to the gate's own mode, with the same
            # shell cap applied below.
            if gate == "shell":
                return level == "gray" and _shell_auto(gates["shell"], level)
            return gates[gate] == "auto"
        # interactive
        if gate == "shell":
            return _shell_auto(gates["shell"], level)
        return gates[gate] == "auto"
    except Exception:  # noqa: BLE001 — a policy failure must ask, never open
        return False


def judge_enabled(conn, which: str) -> bool:
    """Is the shell / egress LLM judge enabled? Unknown `which` → True (the
    judge is itself a safety layer; failing toward running it is the safe
    side)."""
    try:
        return bool(load(conn)["judges"].get(which, True))
    except Exception:  # noqa: BLE001
        return True


def context_mode(conn, ctx: str) -> str:
    """The configured mode for a non-interactive context ("tasks"/"channels")."""
    try:
        mode = load(conn)["contexts"].get(ctx, "strict")
        return mode if mode in CONTEXT_MODES else "strict"
    except Exception:  # noqa: BLE001
        return "strict"


def proxy_settings(conn) -> dict:
    """TOFU TTL / upload threshold for the egress-proxy spawn argv."""
    try:
        return dict(load(conn)["proxy"])
    except Exception:  # noqa: BLE001
        return dict(_PROXY_DEFAULT)
