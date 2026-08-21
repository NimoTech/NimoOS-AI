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
import os
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


# Single-slot in-process cache, keyed by connection IDENTITY (`is`, never
# id() — a recycled id from a garbage-collected test connection must not serve
# a stale policy). The policy only changes through save() in this process, so
# save() refreshes the slot; a different connection simply misses and reads.
# load() runs on the event loop for every gate check (shell, MCP, fs, proxy
# callback), so the hot path must not do SQLite I/O each time — the same
# reasoning as phoenix_tracing's in-process cache.
_cache_conn = None
_cache_doc: dict | None = None


def load(conn) -> dict:
    """The effective policy (defaults merged). Never raises. Returns a fresh
    copy every time — callers may mutate their copy without poisoning the
    cache."""
    global _cache_conn, _cache_doc
    if conn is _cache_conn and _cache_doc is not None:
        return _merge(_cache_doc)
    try:
        row = conn.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key=?",
            (_GLOBAL_SCOPE, _KEY),
        ).fetchone()
    except Exception:  # noqa: BLE001 — a broken read must degrade to defaults
        return default_policy()
    if not row:
        doc = default_policy()
    else:
        try:
            doc = _merge(json.loads(row["value"]))
        except (ValueError, TypeError):
            doc = default_policy()
    _cache_conn, _cache_doc = conn, doc
    return _merge(doc)


def save(conn, doc: dict) -> dict:
    """Normalize + persist. Returns the normalized document (what load() will
    now say), so the API can echo the truth rather than the request."""
    global _cache_conn, _cache_doc
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
    _cache_conn, _cache_doc = conn, normalized
    return _merge(normalized)


def _shell_auto(mode: str, level: str) -> bool:
    """Does shell mode `mode` cover a command classified at `level`?
    `protected` is never covered — in any mode, in any context."""
    if mode == "auto_gray":
        return level == "gray"
    if mode == "auto_all":
        return level in ("gray", "dangerous")
    return False


def decide(policy: dict, gate: str | None, *, level: str = "",
           context: str = "interactive") -> bool:
    """The pure decision core: does `policy` waive the card for `gate`?

    This is the ONLY place the strict/follow/auto ladder and the shell caps
    live — auto_approve, TaskRunDriver and the channel router all call it, so
    the three surfaces cannot drift. `level` is the shell_guard
    classification, meaningful only for the shell gate. An unrecognized
    `context` is never-auto (failing toward "interactive" would pick the MOST
    permissive context). Never raises: any failure means False (ask).
    """
    try:
        if gate is None:
            return False
        gates = (policy or {}).get("gates") or {}
        if context == "interactive":
            if gate == "shell":
                return _shell_auto(gates.get("shell", "ask"), level)
            return gates.get(gate) == "auto"
        if context in ("task", "channel"):
            cmode = ((policy or {}).get("contexts") or {}).get(
                "tasks" if context == "task" else "channels", "strict")
            if cmode not in ("follow", "auto"):
                return False
            # Unattended/remote contexts cap shell at gray in EVERY mode:
            # nobody is watching a scheduled run, and a channel user gets a
            # button for anything above gray instead of silence.
            if gate == "shell":
                if level != "gray":
                    return False
                return cmode == "auto" or _shell_auto(gates.get("shell", "ask"),
                                                      level)
            if cmode == "auto":
                return True
            return gates.get(gate) == "auto"
        return False
    except Exception:  # noqa: BLE001 — a policy failure must ask, never open
        return False


def auto_approve(conn, action: str, *, level: str = "",
                 context: str | None = None) -> bool:
    """Should the gate for `action` skip its card and proceed?

    `context` overrides RUN_CONTEXT_VAR for callers that answer on behalf of
    a run they do not execute inside (TaskRunDriver, channel router). Never
    raises: any failure means False (ask).
    """
    try:
        gate = gate_of(action)
        if gate is None:
            return False
        ctx = context if context is not None else RUN_CONTEXT_VAR.get()
        return decide(load(conn), gate, level=level, context=ctx)
    except Exception:  # noqa: BLE001 — a policy failure must ask, never open
        return False


def egress_gate_action(reason: str) -> str | None:
    """Map the egress-proxy's confirm reason to the action string whose gate
    governs it. THE single copy of this mapping — main.py's callback, the
    channel router and the task driver all use it. Unknown reasons map to
    None (keep the card / deny): a new proxy reason must never inherit the
    network gate's waiver by default.
    """
    if reason == "tofu_unknown_host":
        return "egress"          # → gates.network
    if reason == "upload_over_threshold":
        return "egress_upload"   # → gates.upload
    return None


def gate_of_event(ev: dict) -> tuple[str | None, str]:
    """(gate, shell level) for a CONFIRMATION EVENT dict, matching what the
    emitting gate would have asked. Consumers that answer cards they did not
    emit (TaskRunDriver, channel router) must use this, not gate_of on
    `action` alone: MCP tool/install and toolbox events carry only `kind`,
    egress events need their `reason`, and elicitation events must map to
    None (never waivable) whatever a context mode says.
    """
    if not isinstance(ev, dict):
        return None, ""
    if ev.get("type") == "access_request":
        return "fs_access", ""
    action = ev.get("action") or ""
    if action == "egress_confirm":
        ga = egress_gate_action(str(ev.get("reason") or ""))
        return (gate_of(ga) if ga else None), ""
    kind = ev.get("kind") or ""
    if kind == "mcp_tool":
        return "mcp_tools", ""
    if kind in ("mcp_install", "toolbox_install"):
        return "installs", ""
    if kind.startswith("mcp_elicit"):
        return None, ""
    return gate_of(action), str(ev.get("risk_level") or "")


def paths_policy_grantable(paths) -> bool:
    """Hard floor for POLICY-driven fs auto-grants: every path must be a
    non-empty string whose realpath is outside FS_DENY_ROOTS (and not `/`).
    Only a human click may open a system location; an "auto" policy never
    does. THE single copy — fs/access_request, the channel router and the
    task driver all call it. Never raises; anything invalid means False.
    """
    try:
        if isinstance(paths, str) or not isinstance(paths, (list, tuple)) \
                or not paths:
            return False
        from tasks.driver import fs_root_denied  # noqa: PLC0415 — avoid cycle
        for p in paths:
            if not isinstance(p, str) or not p:
                return False
            if fs_root_denied(os.path.realpath(p)):
                return False
        return True
    except Exception:  # noqa: BLE001 — fail toward asking
        return False


def policy_waives(action: str, *, level: str = "", audit_event: str | None = None,
                  **audit_fields) -> bool:
    """One-stop policy waiver for the simple skill gates (notes, wiki, apps,
    message-bus, MCP admin/tools, toolbox): consults the policy on the
    process connection and, when waived, writes the audit record with
    decision="auto_approved_by_policy". Never raises; any failure means
    False (raise the card). Gates with special conn/floor semantics (shell,
    fs, the proxy callback) keep their own wiring.
    """
    try:
        import db as _dbmod  # noqa: PLC0415 — lazy, mirrors the skills' pattern
        if not auto_approve(_dbmod.get_connection(), action, level=level):
            return False
        try:
            from audit import audit as _audit  # noqa: PLC0415
            _audit(audit_event or action,
                   decision="auto_approved_by_policy", **audit_fields)
        except Exception:  # noqa: BLE001 — auditing must never break the waiver
            pass
        return True
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
