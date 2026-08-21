"""Deterministic shell command risk classifier.

Priority per segment: dangerous/protected > gray > safe. Across segments the
worst level wins. Unparseable input (parse.segments -> None) is GRAY, never SAFE.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from shell_guard.parse import Segment, extract_paths, segments

# ── SAFE read-only command names ──────────────────────────────────────────────
_SAFE_CMDS = {
    "ls", "tree", "cat", "head", "tail", "file", "grep", "egrep", "fgrep",
    "ps", "df", "du", "wc", "stat", "realpath", "basename", "dirname",
    "pwd", "which", "echo", "env", "printenv", "id", "uname", "hostname",
    "date", "cksum", "sha256sum", "md5sum",
}
# git subcommands that are read-only
_SAFE_GIT_SUB = {"status", "log", "diff", "show"}

# Write-redirect targets that are sinks, not files: redirecting into them
# neither persists data nor damages a device.
_HARMLESS_DEVS = ("/dev/null", "/dev/stdout", "/dev/stderr")

# ── DANGEROUS verb/flag patterns ──────────────────────────────────────────────
_SHELL_CMDS = {"sh", "bash", "zsh", "dash"}
_DISK_CMDS = {"dd", "mkfs", "wipefs", "fdisk", "parted", "sgdisk", "shred"}
_SYSTEMCTL_MUTATE = {"start", "stop", "restart", "reload", "enable", "disable", "mask",
                     "unmask", "kill", "isolate", "poweroff", "reboot", "halt",
                     "suspend", "hibernate", "daemon-reload", "set-default"}
_APT_MUTATE = {"install", "remove", "purge", "autoremove", "upgrade",
               "full-upgrade", "dist-upgrade"}
_YUM_MUTATE = {"install", "remove", "erase", "update", "upgrade", "downgrade"}


def _cmd_name(seg: Segment) -> str:
    return os.path.basename(seg.argv[0]) if seg.argv else ""


# ── exec-wrapper / assignment unwrapping ──────────────────────────────────────
# argv[0] alone does NOT tell us what runs: `env CMD`, `nohup CMD`, `X=1 CMD`
# all execute CMD while presenting a benign argv[0]. Classifying on the raw
# argv[0] let `env rm -rf /DATA` slip through as SAFE (2026-07-16 review). We
# strip assignment prefixes and known exec-wrappers to reveal the command that
# actually runs, then classify THAT. Best-effort and fail-safe: whatever we
# cannot resolve stays as-is and lands in GRAY (never SAFE).
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_EXEC_WRAPPERS = {
    "env", "nohup", "nice", "timeout", "stdbuf", "setsid", "ionice",
    "sudo", "doas", "xargs", "chroot",
}
# Wrappers whose first non-option operand is NOT the command (must be skipped):
# timeout DURATION CMD, chroot DIR CMD.
_WRAPPERS_WITH_OPERAND = {"timeout", "chroot"}


def _effective_argv(argv: list[str]) -> list[str]:
    a = list(argv)
    for _ in range(8):  # bounded: guards against pathological nesting
        # strip leading VAR=VAL assignment tokens
        j = 0
        while j < len(a) and _ENV_ASSIGN_RE.match(a[j]):
            j += 1
        if j:
            a = a[j:]
            continue
        if not a:
            break
        name = os.path.basename(a[0])
        if name in _EXEC_WRAPPERS:
            rest = a[1:]
            k = 0
            while k < len(rest) and rest[k].startswith("-"):
                k += 1
            if name in _WRAPPERS_WITH_OPERAND and k < len(rest):
                k += 1  # skip the mandatory operand (duration / dir)
            inner = rest[k:]
            if not inner:
                # No inner command (bare `env`, `env -i`): the wrapper is
                # running in its degenerate/print mode — leave it as-is so it
                # classifies via its own name (e.g. `env`/`printenv` = SAFE).
                break
            a = inner
            continue
        break
    return a


def _effective_seg(seg: Segment) -> Segment:
    return Segment(argv=_effective_argv(seg.argv),
                   redirect_targets=seg.redirect_targets,
                   read_targets=seg.read_targets)


# A SAFE command name is only trusted when argv[0] is a bare name (resolved via
# PATH) or lives in a standard system bin dir. `/tmp/ls`, `./ls`, a downloaded
# binary named `cat` — anything path-qualified outside these dirs — is not the
# system tool it names, so it must not inherit SAFE.
_TRUSTED_BIN_DIRS = ("/bin/", "/usr/bin/", "/usr/local/bin/", "/sbin/", "/usr/sbin/")


def _is_trusted_argv0(arg0: str) -> bool:
    if "/" not in arg0:
        return True
    return any(arg0.startswith(d) for d in _TRUSTED_BIN_DIRS)


def _is_dangerous_seg(seg: Segment, all_segs: list[Segment], idx: int) -> str | None:
    name = _cmd_name(seg)
    flags = [a for a in seg.argv[1:] if a.startswith("-")]
    flagchars = "".join(f.lstrip("-") for f in flags)

    if name == "rm" and ("r" in flagchars.lower() or "f" in flagchars.lower()):
        return "recursive/forced remove"
    if name == "find" and "-delete" in seg.argv:
        return "find -delete"
    if name in _DISK_CMDS or name.startswith("mkfs"):
        return f"disk/format command: {name}"
    if name in ("chmod", "chown") and "R" in flagchars:
        return f"recursive {name}"
    if name == "systemctl":
        if any(a in _SYSTEMCTL_MUTATE for a in seg.argv[1:]):
            return "systemctl mutating command"
        return None
    if name in ("apt", "apt-get"):
        if any(a in _APT_MUTATE for a in seg.argv[1:]):
            return f"{name} mutating command"
        return None
    if name in ("yum", "dnf"):
        if any(a in _YUM_MUTATE for a in seg.argv[1:]):
            return f"{name} mutating command"
        return None
    if name == "dpkg":
        if any(f in seg.argv[1:] for f in ("-r", "-P", "-i", "--remove", "--purge", "--install")):
            return "dpkg mutating command"
        return None
    if name == "docker" and any(x in seg.argv for x in ("prune", "rm", "rmi")):
        return "docker destructive subcommand"
    # pipe-to-shell: this segment is a shell AND a previous segment fetches remotely
    if name in _SHELL_CMDS and idx > 0:
        prev = _cmd_name(all_segs[idx - 1])
        if prev in ("curl", "wget", "fetch"):
            return "pipe remote content to shell"
    # bash -c "$(...)" is already caught as unparseable by parse (returns gray),
    # but a literal 'bash -c curl...' still flags:
    if name in _SHELL_CMDS and "-c" in seg.argv:
        joined = " ".join(seg.argv)
        if re.search(r"\bcurl\b|\bwget\b", joined):
            return "shell -c fetching remote content"
    # fork bomb heuristic
    if ":(){" in "".join(seg.argv):
        return "fork bomb pattern"
    # redirect to a device node. /dev/null (and the std streams, which are
    # pipe-equivalent) is exempt: `2>/dev/null` is everyday noise-silencing,
    # and flagging it DANGEROUS produced pure false alarms (`ls 2>/dev/null`
    # was a confirmation card before 2026-08-21).
    for tgt in seg.redirect_targets:
        if tgt.startswith("/dev/") and tgt not in _HARMLESS_DEVS:
            return f"redirect to device: {tgt}"
    return None


# ── PROTECTED path prefixes ───────────────────────────────────────────────────
# Two classes, one behavioral split (2026-08-21, user-requested read/write
# distinction):
#
# * SECRET paths — the harm is READING them (confidentiality): credential
#   files, key material, the agent's own database (MCP tokens, channel
#   credentials), /etc (shadow, ssl private keys, service configs) and
#   /var/lib/nimoos (service databases: user.db password hashes, JWT state).
#   Any access, read or write, stays PROTECTED.
# * INTEGRITY paths — the harm is WRITING them: system/application code that
#   is world-readable anyway (/usr, /boot, /opt/nimoos). A read-only trusted
#   command touching them downgrades to GRAY (see _classify_seg) — never
#   SAFE, so the touch still lands in the audit trail and still meets the
#   judge/card under a strict policy.
_SECRET_PREFIXES = (
    "/etc", "/var/lib/nimoos",
)
_INTEGRITY_PREFIXES = (
    "/boot", "/usr", "/opt/nimoos",
)
_PROTECTED_PREFIXES = _SECRET_PREFIXES + _INTEGRITY_PREFIXES
_PROTECTED_SUFFIXES = (".key", ".pem")
_PROTECTED_SUBSTR = ("/.ssh/", "agent.db")
_DATA_ROOT = "/DATA"

# Commands whose normal operation only READS its path operands. Deliberately
# a hand-picked list, not "everything that seems harmless": each entry must
# not be able to modify or execute its operands through ordinary argv usage.
# Interpreters, editors, and copy/移动 verbs never belong here. sed and find
# are conditional — see _seg_reads_only.
_READONLY_CMDS = _SAFE_CMDS | {
    "less", "more", "strings", "od", "xxd", "hexdump", "diff", "cmp",
    "sort", "uniq", "cut", "tr", "nl", "tac", "readlink", "rg", "jq",
    "column", "find", "sed",
}

# find operands that make it write/execute instead of just walking.
_FIND_MUTATING = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}


def _seg_reads_only(eff: Segment) -> bool:
    """True if this (effective) segment can only READ its path operands.

    Fail toward False: anything not positively known read-only keeps the
    stricter classification.
    """
    if not eff.argv:
        return False
    name = _cmd_name(eff)
    if name not in _READONLY_CMDS or not _is_trusted_argv0(eff.argv[0]):
        return False
    if name == "sed":
        # -i / --in-place edits the file; every other common sed flag (-n, -e,
        # -E, -r, -s, -z, -u) is read-side. A combined bundle like -ni still
        # carries the i, so scan flag tokens for it.
        for a in eff.argv[1:]:
            if a == "--in-place" or a.startswith("--in-place="):
                return False
            if a.startswith("-") and not a.startswith("--") and "i" in a[1:]:
                return False
    if name == "find":
        if any(a in _FIND_MUTATING or a.startswith("-fprint") for a in eff.argv[1:]):
            return False
    return True


def _resolve(p: str, cwd: str | None = None) -> str:
    # A relative path must resolve against the command's EXECUTION cwd (the
    # session work dir), NOT the classifier process's own cwd — otherwise
    # `../../etc/x` resolves to a different target than where it will be written.
    try:
        if cwd and not os.path.isabs(p):
            p = os.path.join(cwd, p)
        return os.path.realpath(p)
    except OSError:
        return p


def _is_protected_path(raw: str, cwd: str | None = None) -> bool:
    rp = _resolve(raw, cwd)
    if any(rp == pre or rp.startswith(pre + "/") for pre in _PROTECTED_PREFIXES):
        return True
    if rp.endswith(_PROTECTED_SUFFIXES):
        return True
    if any(s in rp for s in _PROTECTED_SUBSTR):
        return True
    return False


def _is_secret_path(raw: str, cwd: str | None = None) -> bool:
    """Confidentiality-class protected path: READING it is already the harm,
    so no read-only downgrade ever applies (agent.db, keys, /etc, ...)."""
    rp = _resolve(raw, cwd)
    if any(rp == pre or rp.startswith(pre + "/") for pre in _SECRET_PREFIXES):
        return True
    if rp.endswith(_PROTECTED_SUFFIXES):
        return True
    return any(s in rp for s in _PROTECTED_SUBSTR)


def _is_data_mass(raw: str, cwd: str | None = None) -> bool:
    if "*" in raw and (raw == "/DATA/*" or raw.startswith("/DATA/")):
        return True
    return _resolve(raw.rstrip("/"), cwd) == _DATA_ROOT


def _is_destructive(seg: Segment) -> bool:
    name = _cmd_name(seg)
    if name in ("rm", "shred"):
        return True
    if name == "find" and "-delete" in seg.argv:
        return True
    return False


# ── Result ────────────────────────────────────────────────────────────────────
@dataclass
class Decision:
    level: str  # "safe" | "gray" | "dangerous" | "protected"
    reason: str = ""
    paths: list[str] = field(default_factory=list)


_RANK = {"safe": 0, "gray": 1, "dangerous": 2, "protected": 3}


def _worse(a: Decision, b: Decision) -> Decision:
    return a if _RANK[a.level] >= _RANK[b.level] else b


def _classify_seg(seg: Segment, all_segs: list[Segment], idx: int,
                  cwd: str | None = None) -> Decision:
    # Classify on the EFFECTIVE command (wrappers/assignments stripped), so
    # `env rm -rf /DATA` is judged as the `rm` it actually runs, not as `env`.
    eff = _effective_seg(seg)
    name = _cmd_name(eff)
    paths = extract_paths(eff)

    danger = _is_dangerous_seg(eff, all_segs, idx)

    sensitive = [p for p in paths if _is_protected_path(p, cwd)]
    if sensitive:
        # Read/write split (2026-08-21): only INTEGRITY-class paths touched by
        # a positively read-only trusted command downgrade. Anything else —
        # a secret-class path (reading is the harm), a redirect INTO any
        # protected path (a write, whatever the verb), a dangerous pattern,
        # or a command we can't prove read-only — stays PROTECTED.
        secret = [p for p in sensitive if _is_secret_path(p, cwd)]
        redirected = [t for t in seg.redirect_targets
                      if _is_protected_path(t, cwd)]
        if secret or redirected or danger or not _seg_reads_only(eff):
            return Decision("protected",
                            f"touches protected path(s): {sensitive}", sensitive)
        # GRAY, never SAFE: the touch stays in the audit trail and, under a
        # strict policy, still meets the judge/confirmation card.
        return Decision("gray",
                        f"read-only access to protected path(s): {sensitive}",
                        sensitive)

    # /DATA mass op: escalate only for destructive commands (avoid over-blocking reads)
    if _is_destructive(eff):
        mass = [p for p in paths if _is_data_mass(p, cwd)]
        if mass:
            return Decision("protected", f"mass delete under /DATA: {mass}", mass)

    if danger:
        return Decision("dangerous", danger, paths)

    # /dev/null-family sinks don't make a reader a writer (`ls 2>/dev/null`).
    has_write_redirect = any(t not in _HARMLESS_DEVS
                             for t in seg.redirect_targets)
    arg0 = eff.argv[0] if eff.argv else ""
    if name in _SAFE_CMDS and not has_write_redirect and _is_trusted_argv0(arg0):
        return Decision("safe")
    if name == "git" and len(eff.argv) > 1 and eff.argv[1] in _SAFE_GIT_SUB \
            and not has_write_redirect and _is_trusted_argv0(arg0):
        return Decision("safe")

    return Decision("gray", f"unclassified command: {name or '(redirect)'}", paths)


def classify(command: str, cwd: str | None = None) -> Decision:
    segs = segments(command)
    if segs is None:
        return Decision("gray", "command could not be parsed (obfuscation/substitution)")
    if not segs:
        return Decision("safe")
    result = Decision("safe")
    all_paths: list[str] = []
    for idx, seg in enumerate(segs):
        result = _worse(result, _classify_seg(seg, segs, idx, cwd))
        for p in extract_paths(seg):
            if p not in all_paths:
                all_paths.append(p)
    if result.level != "safe":
        result.paths = all_paths
    return result
