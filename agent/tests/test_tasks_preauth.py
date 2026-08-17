"""Run-scoped pre-authorization (scheduled tasks M2, Task 2).

Covers the pure rule matcher (`tasks.preauth`) and the two gates it feeds:
the shell guard's run-scoped allowlist and the MCP per-server wildcard
pre-confirmation.  The security-critical assertions are the NEGATIVE ones:
a run-scoped grant must never widen the gate beyond a single simple command,
and must never cover a `protected`-level command.
"""
import asyncio
import time

import pytest

import db as dbmod
import shell_guard
import mcp_client.client as mc
from confirm import ConfirmManager
from skills import shell


# ── tasks.preauth: pure normalization + matching ──────────────────────────────

def test_parse_normalizes_and_drops_junk():
    from tasks import preauth
    p = preauth.parse({"shell": [{"kind": "prefix", "value": "lark-cli "},
                                 {"kind": "bogus", "value": "x"}, "not-a-dict"],
                       "egress_domains": ["open.feishu.cn", 42],
                       "mcp_tools": ["srv::*"], "fs_write": ["/DATA/x"],
                       "unknown_key": [1]})
    assert p["shell"] == [{"kind": "prefix", "value": "lark-cli "}]
    assert p["egress_domains"] == ["open.feishu.cn"]
    assert p["mcp_tools"] == ["srv::*"] and p["fs_write"] == ["/DATA/x"]
    assert "unknown_key" not in p


def test_parse_accepts_json_string_and_bad_json():
    from tasks import preauth
    assert preauth.parse('{"shell": []}')["shell"] == []
    assert preauth.parse("not json")["shell"] == []


def test_parse_tolerates_none_and_wrong_shapes():
    from tasks import preauth
    for junk in (None, [], 7, {"shell": "lark-cli "}, {"egress_domains": "abc"}):
        p = preauth.parse(junk)
        assert p == {"shell": [], "egress_domains": [],
                     "mcp_tools": [], "fs_write": []}


def test_shell_match_prefix_and_regex():
    from tasks import preauth
    rules = [{"kind": "prefix", "value": "lark-cli "},
             {"kind": "regex", "value": r"^gh (pr|issue) list"}]
    assert preauth.shell_match(rules, "lark-cli im chats list")
    assert preauth.shell_match(rules, "gh pr list --limit 5")
    assert not preauth.shell_match(rules, "rm -rf /DATA")
    assert not preauth.shell_match(rules, "echo lark-cli ")  # 前缀不是子串


def test_shell_match_bad_regex_is_not_a_match():
    from tasks import preauth
    assert not preauth.shell_match([{"kind": "regex", "value": "("}], "anything")


def test_shell_match_regex_is_anchored_at_start():
    """An unanchored pattern must not vouch for a command that merely CONTAINS
    it — matching is start-anchored regardless of what the rule author wrote."""
    from tasks import preauth
    rules = [{"kind": "regex", "value": r"gh pr list"}]
    assert preauth.shell_match(rules, "gh pr list")
    assert not preauth.shell_match(rules, "rm -rf /DATA && gh pr list")


def test_shell_match_empty_rules_and_junk():
    from tasks import preauth
    assert not preauth.shell_match([], "ls")
    assert not preauth.shell_match(None, "ls")
    assert not preauth.shell_match(["nope", {"kind": "prefix"}, {"value": "ls"}], "ls")


# ── MCP wildcard pre-confirmation ─────────────────────────────────────────────

def test_mcp_wildcard_membership():
    """契约测试:通配键形态与 _ensure_confirmed 的判断一致。"""
    s = {"srv1::*", "srv2::search"}
    assert ("srv1::" + "anything") not in s          # 精确键不在
    assert "srv1::*" in s                             # 通配键在


META = {"name": "search", "description": "does a thing",
        "input_schema": {"type": "object",
                         "properties": {"q": {"type": "string"}}}}


class _FakeQueue:
    def __init__(self):
        self.events = []

    async def put(self, e):
        self.events.append(e)


class _FakeConn:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))

        class Block:
            type = "text"
            text = "RESULT"

        class Res:
            content = [Block()]
            isError = False
        return Res()

    async def aclose(self):
        pass


def _mcp_setup(conn=None):
    import sqlite3
    sconn = sqlite3.connect(":memory:")
    sconn.execute("CREATE TABLE pending_confirmations (confirm_id TEXT, "
                  "session_id TEXT, action TEXT, description TEXT, "
                  "command TEXT, created_at INT)")
    mgr = ConfirmManager(sconn, timeout=5)
    q = _FakeQueue()
    mc.SESSION_ID_VAR.set("s1")
    mc.EVENT_QUEUE_VAR.set(q)
    mc.CONFIRM_MGR_VAR.set(mgr)
    mc.USER_PATTERNS_VAR.set([])
    mc._CONFIRMED_TOOLS_VAR.set(set())
    mc._RUN_CONNS_VAR.set({1: conn} if conn else {})
    mc._RUN_CONN_LOCKS_VAR.set({})
    return mgr, q


@pytest.mark.asyncio
async def test_mcp_server_wildcard_skips_confirmation():
    """`<server_id>::*` in the pre-confirmed set waives confirmation for ANY
    tool on that server (and only that server)."""
    fconn = _FakeConn()
    _mgr, q = _mcp_setup(conn=fconn)
    mc._CONFIRMED_TOOLS_VAR.set({"1::*"})
    ok = await mc._ensure_confirmed({"id": 1, "name": "git"}, "search", {"q": "hi"})
    assert ok is True
    assert q.events == []   # nothing was asked


@pytest.mark.asyncio
async def test_mcp_wildcard_of_other_server_still_confirms():
    """A wildcard for a different server must NOT waive this server's tools."""
    _mgr, q = _mcp_setup(conn=_FakeConn())
    mc._CONFIRMED_TOOLS_VAR.set({"2::*"})

    async def deny():
        for _ in range(50):
            if q.events:
                break
            await asyncio.sleep(0.01)
        _mgr.resolve(q.events[-1]["confirm_id"], confirmed=False,
                     expected_session_id="s1")

    asyncio.create_task(deny())
    ok = await mc._ensure_confirmed({"id": 1, "name": "git"}, "search", {"q": "hi"})
    assert ok is False
    assert q.events                  # it DID ask


@pytest.mark.asyncio
async def test_mcp_global_star_is_inert():
    """Only `<server_id>::*` is a wildcard. A bare `*` must vouch for nothing."""
    _mgr, q = _mcp_setup(conn=_FakeConn())
    mc._CONFIRMED_TOOLS_VAR.set({"*"})

    async def deny():
        for _ in range(50):
            if q.events:
                break
            await asyncio.sleep(0.01)
        _mgr.resolve(q.events[-1]["confirm_id"], confirmed=False,
                     expected_session_id="s1")

    asyncio.create_task(deny())
    ok = await mc._ensure_confirmed({"id": 1, "name": "git"}, "search", {"q": "hi"})
    assert ok is False
    assert q.events


@pytest.mark.asyncio
async def test_mcp_exact_key_still_works():
    _mgr, q = _mcp_setup(conn=_FakeConn())
    mc._CONFIRMED_TOOLS_VAR.set({"1::search"})
    assert await mc._ensure_confirmed({"id": 1, "name": "git"}, "search", {}) is True
    assert q.events == []


# ── shell run-scoped allowlist ────────────────────────────────────────────────

class _Mgr:
    """Minimal fake ConfirmManager (mirrors tests/test_shell_guard_gate.py)."""
    def __init__(self, grant, remember=False):
        self._grant, self._remember = grant, remember
        self.registered = []

    def register(self, sid, action, desc, command):
        self.registered.append((action, command))
        return "cid-1"

    async def wait(self, cid):
        return self._grant

    def consume_remember(self, cid):
        return self._remember


class _Sink:
    def __init__(self):
        self.events = []

    async def put(self, ev):
        self.events.append(ev)


@pytest.fixture(autouse=True)
def _reset_run_allowlist():
    """ContextVars set in a sync test leak into later tests in the same
    process — always hand the gate back an empty run allowlist."""
    yield
    shell.RUN_ALLOWLIST_VAR.set(())


def _shell_setup(monkeypatch, mgr=None, sink=None, unattended=True):
    conn = dbmod.init_db(":memory:")
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) "
                 "VALUES ('s1','u1',0,0)")
    conn.commit()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)
    shell.CONFIRM_MGR_VAR.set(None if unattended else mgr)
    shell.EVENT_QUEUE_VAR.set(None if unattended else sink)
    shell.RUN_ALLOWLIST_VAR.set(())
    monkeypatch.setattr("shell_guard.backstop.prepare_backstop",
                        lambda paths, trash_root=None: __import__(
                            "shell_guard.backstop", fromlist=["BackstopResult"]
                        ).BackstopResult("none", "", False, ""))
    return conn


def test_run_allowlist_allows_gray_unattended(monkeypatch):
    """A run-scoped prefix rule lets a gray command through unattended, without
    even consulting the judge."""
    _shell_setup(monkeypatch)

    async def _boom(_cmd):
        raise AssertionError("judge must not be consulted for a pre-authorized command")
    monkeypatch.setattr(shell, "judge_command", _boom)

    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": "lark-cli "}])
    assert asyncio.run(shell._guard_command("lark-cli im chats list")) is None


def test_run_allowlist_allows_dangerous_and_keeps_backstop(monkeypatch):
    """Dangerous is allowed (same as the persistent allowlist) but the backstop
    is still built — a pre-approved rm keeps its recoverable snapshot."""
    _shell_setup(monkeypatch)
    calls = []

    def _fake_backstop(paths, trash_root=None):
        calls.append(paths)
        from shell_guard.backstop import BackstopResult
        return BackstopResult("none", "", False, "")
    monkeypatch.setattr("shell_guard.backstop.prepare_backstop", _fake_backstop)

    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": "rm -rf /DATA/x"}])
    assert asyncio.run(shell._guard_command("rm -rf /DATA/x")) is None
    assert len(calls) == 1


def test_run_allowlist_never_covers_protected(monkeypatch):
    """RED LINE: a `protected`-level command is refused even when a run rule
    matches it — deliberately STRICTER than the persistent allowlist, which
    does pass mass-delete-under-/DATA through a prefix entry."""
    conn = _shell_setup(monkeypatch)
    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": "rm -rf /DATA/"},
                                 {"kind": "prefix", "value": "cat /etc/"}])
    for cmd in ("rm -rf /DATA/*", "cat /etc/shadow"):
        msg = asyncio.run(shell._guard_command(cmd))
        assert msg is not None and "NOT executed" in msg, cmd

    # Control: the PERSISTENT allowlist does pass the same mass-delete —
    # documenting the asymmetry so a future refactor can't silently level it.
    from shell_guard import allowlist as AL
    AL.add(conn, "prefix", "rm -rf /DATA/", "user")
    assert AL.match(conn, "rm -rf /DATA/*") is True


def test_run_allowlist_rejects_compound_command(monkeypatch):
    """A matching prefix must not vouch for a chained destructive tail."""
    _shell_setup(monkeypatch)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": "lark-cli "}])
    msg = asyncio.run(shell._guard_command("lark-cli im send; rm -rf /DATA/important"))
    assert msg is not None and "no confirmation channel" in msg


def test_run_allowlist_rejects_redirect(monkeypatch):
    """Redirection is smuggling too (`lark-cli x > /DATA/important`)."""
    _shell_setup(monkeypatch)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": "lark-cli "}])
    msg = asyncio.run(shell._guard_command("lark-cli dump > /DATA/notes.txt"))
    assert msg is not None and "no confirmation channel" in msg


def test_empty_run_allowlist_leaves_behavior_unchanged(monkeypatch):
    """Bit-identical default: no rules → the gate behaves exactly as before."""
    _shell_setup(monkeypatch)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    assert not shell.RUN_ALLOWLIST_VAR.get()
    assert asyncio.run(shell._guard_command("ls -la")) is None       # safe
    msg = asyncio.run(shell._guard_command("rm -rf /DATA/x"))        # dangerous
    assert msg is not None and "no confirmation channel" in msg


def test_run_allowlist_does_not_write_persistent_allowlist(monkeypatch):
    """A run-scoped grant is ephemeral: it must never land in shell_allowlist."""
    conn = _shell_setup(monkeypatch)
    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": "rm -rf /DATA/x"}])
    asyncio.run(shell._guard_command("rm -rf /DATA/x"))
    from shell_guard import allowlist as AL
    assert AL.list_entries(conn) == []


# ── fix round 1: I1 interpreter escape hatch ─────────────────────────────────

# Each of these hides its real work in a string argument that `classify` cannot
# read — several classify as GRAY, so the `protected` exclusion alone would not
# have stopped them.  A run rule that literally names the interpreter must still
# not vouch for them.
_INTERPRETER_BYPASSES = [
    ("sh -c ", "sh -c \"cat /$(echo etc)/shadow\""),
    ("sh -c ", "sh -c 'rm -rf $HOME'"),
    ("bash -c ", "bash -c 'cat /etc/shadow'"),
    ("python3 -c ", "python3 -c 'import os; os.system(\"rm -rf /DATA/x\")'"),
    ("perl -e ", "perl -e 'unlink glob \"/DATA/*\"'"),
    ("node -e ", "node -e 'require(\"fs\").rmSync(\"/DATA\",{recursive:true})'"),
    ("find /DATA ", "find /DATA -name '*.tmp' -exec rm -f {} ;"),
    # `env`/`nohup`/assignments are unwrapped first, so the interpreter behind
    # them is still recognized.
    ("env python3 -c ", "env python3 -c 'import os; os.system(\"rm -rf /DATA/x\")'"),
    # Round-2 additions: same "argv hides the real work" shape.
    ("pwsh ", "pwsh -Command 'Remove-Item -Recurse /DATA'"),
    ("powershell ", "powershell -Command 'Remove-Item -Recurse /DATA'"),
    ("tclsh ", "tclsh /tmp/payload.tcl"),
    ("expect ", "expect /tmp/payload.exp"),
    ("Rscript ", "Rscript /tmp/payload.R"),
    ("osascript ", "osascript -e 'do shell script \"rm -rf /DATA\"'"),
    ("xargs", "xargs"),
    ("make ", "make deploy"),
    ("systemd-run ", "systemd-run --user rm -rf /DATA/x"),
    ("ansible-playbook ", "ansible-playbook site.yml"),
]


@pytest.mark.parametrize("rule_value,command", _INTERPRETER_BYPASSES)
def test_run_allowlist_never_covers_interpreters(monkeypatch, rule_value, command):
    """I1 RED LINE: an interpreter/exec-flag command is refused even when a run
    rule names it exactly — its payload is invisible to the classifier, so
    pre-authorizing it would be pre-authorizing anything."""
    _shell_setup(monkeypatch)

    async def _ask(_cmd):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": rule_value},
                                 {"kind": "regex", "value": ".*"}])
    msg = asyncio.run(shell._guard_command(command))
    assert msg is not None, command
    assert "NOT executed" in msg or "no confirmation channel" in msg, command


def test_run_allowlist_interpreter_check_is_run_scoped_only(monkeypatch):
    """The persistent allowlist is untouched by the interpreter rule — that is
    existing, human-maintained behavior and a separate follow-up."""
    conn = _shell_setup(monkeypatch)
    from shell_guard import allowlist as AL
    AL.add(conn, "prefix", "sh -c ", "user")
    assert AL.match(conn, "sh -c 'rm -rf $HOME'") is True


def test_run_allowlist_still_passes_a_normal_command(monkeypatch):
    """Control for I1: the interpreter rule must not swallow ordinary tools."""
    _shell_setup(monkeypatch)

    async def _boom(_cmd):
        raise AssertionError("judge must not be consulted")
    monkeypatch.setattr(shell, "judge_command", _boom)

    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": "lark-cli "}])
    assert asyncio.run(shell._guard_command("lark-cli im chats list")) is None


# ── fix round 1: I2 regex cost bounds ────────────────────────────────────────

def test_shell_match_skips_regex_for_overlong_command():
    """I2 (secondary bound): regex rules are skipped past the length cap. The
    pattern here WOULD match — being skipped is the point. This bounds the
    polynomial cases the shape check does not catch; the exponential PoC is
    covered by test_shell_match_redos_poc_is_fast."""
    from tasks import preauth
    rules = [{"kind": "regex", "value": r"^a+b$"}]
    long_cmd = "a" * (preauth.MAX_REGEX_COMMAND_LEN + 1) + "b"
    assert preauth.shell_match(rules, "a" * 10 + "b") is True
    assert preauth.shell_match(rules, long_cmd) is False


def test_shell_match_regex_still_applies_below_the_bound():
    from tasks import preauth
    rules = [{"kind": "regex", "value": r"^gh pr list"}]
    assert preauth.shell_match(rules, "gh pr list --limit 5") is True


def test_shell_match_prefix_unaffected_by_length_bound():
    """Prefix matching is linear — length must not disable it."""
    from tasks import preauth
    rules = [{"kind": "prefix", "value": "lark-cli "}]
    assert preauth.shell_match(rules, "lark-cli " + "x" * 5000) is True


def test_parse_truncates_oversized_rule_lists():
    from tasks import preauth
    doc = {"shell": [{"kind": "prefix", "value": f"c{i} "} for i in range(200)],
           "egress_domains": [f"h{i}.example.com" for i in range(100)]}
    p = preauth.parse(doc)
    assert len(p["shell"]) == preauth.MAX_RULES
    assert len(p["egress_domains"]) == preauth.MAX_RULES
    assert p["shell"][0] == {"kind": "prefix", "value": "c0 "}   # head kept


def test_parse_with_report_surfaces_truncation(monkeypatch):
    """Minor: truncation used to be silent to the author. parse_with_report
    hands the API layer (Task 7) something to show them."""
    from tasks import preauth
    doc, report = preauth.parse_with_report({
        "shell": [{"kind": "prefix", "value": f"c{i} "} for i in range(70)],
        "mcp_tools": ["srv::a"]})
    assert len(doc["shell"]) == preauth.MAX_RULES
    assert report["truncated"]["shell"] == {"kept": preauth.MAX_RULES,
                                           "dropped": 70 - preauth.MAX_RULES}
    assert "mcp_tools" not in report["truncated"]


def test_parse_with_report_is_empty_for_a_clean_document():
    from tasks import preauth
    doc, report = preauth.parse_with_report(
        {"shell": [{"kind": "prefix", "value": "lark-cli "}]})
    assert doc["shell"] and report == {"truncated": {}, "rejected_rules": []}


def test_parse_delegates_to_parse_with_report():
    """parse() keeps its exact four-key shape — no report keys leak into it."""
    from tasks import preauth
    p = preauth.parse({"shell": [{"kind": "prefix", "value": "x "}]})
    assert set(p) == {"shell", "egress_domains", "mcp_tools", "fs_write"}


def test_shell_match_caps_rules_even_if_not_parsed():
    """shell_match is also called with raw rule lists (defense in depth)."""
    from tasks import preauth
    rules = ([{"kind": "prefix", "value": "nope "}] * preauth.MAX_RULES
             + [{"kind": "prefix", "value": "yes "}])
    assert preauth.shell_match(rules, "yes go") is False


# ── fix round 1: I3 audit the MCP wildcard grant ─────────────────────────────

@pytest.mark.asyncio
async def test_mcp_wildcard_grant_is_audited(tmp_path):
    """I3: a wildcard grant skips the confirmation card, so without this the
    call would leave no trace anywhere."""
    import json
    import audit as A
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    try:
        _mgr, q = _mcp_setup(conn=_FakeConn())
        mc._CONFIRMED_TOOLS_VAR.set({"1::*"})
        assert await mc._ensure_confirmed(
            {"id": 1, "name": "git"}, "search", {"q": "hi"}) is True
        recs = [json.loads(line) for line
                in (tmp_path / "audit.log").read_text().splitlines()]
    finally:
        A.set_audit_path_for_test(None)
    hits = [r for r in recs if r["event"] == "mcp_call"]
    assert len(hits) == 1
    assert hits[0]["reason"] == "run-preauth-wildcard"
    assert hits[0]["tool"] == "search" and hits[0]["server"] == 1
    assert q.events == []


@pytest.mark.asyncio
async def test_mcp_wildcard_audit_carries_user_id(tmp_path):
    """Minor: align with the shell audit record, which always names the user."""
    import json
    import audit as A
    from skills.skills_registry import USER_ID_VAR
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    token = USER_ID_VAR.set("u42")
    try:
        _mcp_setup(conn=_FakeConn())
        mc._CONFIRMED_TOOLS_VAR.set({"1::*"})
        await mc._ensure_confirmed({"id": 1, "name": "git"}, "search", {})
        recs = [json.loads(line) for line
                in (tmp_path / "audit.log").read_text().splitlines()]
    finally:
        USER_ID_VAR.reset(token)
        A.set_audit_path_for_test(None)
    hit = next(r for r in recs if r["event"] == "mcp_call")
    assert hit["user_id"] == "u42"
    assert hit["session_id"] == "s1"


@pytest.mark.asyncio
async def test_mcp_exact_confirmation_is_not_audited_as_wildcard(tmp_path):
    """Control: the pre-existing exact-key path must not start emitting the
    new record (it is covered by the confirmation trail)."""
    import json
    import audit as A
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    try:
        _mgr, _q = _mcp_setup(conn=_FakeConn())
        mc._CONFIRMED_TOOLS_VAR.set({"1::search"})
        assert await mc._ensure_confirmed({"id": 1, "name": "git"}, "search", {}) is True
        logf = tmp_path / "audit.log"
        recs = ([json.loads(line) for line in logf.read_text().splitlines()]
                if logf.exists() else [])
    finally:
        A.set_audit_path_for_test(None)
    assert [r for r in recs if r["event"] == "mcp_call"] == []


# ── fix round 1: Minor5 immutable ContextVar default ─────────────────────────

# ── fix round 2: M1 the regex bound must stop the ACTUAL PoC ─────────────────

_REDOS_POC = r"^(a+)+$"
_REDOS_INPUT = "a" * 30 + "b"     # 31 chars — blocks for ~29 s unguarded


def test_shell_match_redos_poc_is_fast():
    """M1: the finding's own PoC. Unguarded this blocks the event loop ~29 s;
    the round-1 512-char input cap did nothing for it (31-char input)."""
    from tasks import preauth
    t0 = time.perf_counter()
    assert preauth.shell_match([{"kind": "regex", "value": _REDOS_POC}],
                               _REDOS_INPUT) is False
    assert time.perf_counter() - t0 < 0.1


def test_parse_rejects_redos_poc_and_reports_it():
    from tasks import preauth
    doc, report = preauth.parse_with_report({"shell": [
        {"kind": "regex", "value": _REDOS_POC},
        {"kind": "prefix", "value": "lark-cli "}]})
    assert doc["shell"] == [{"kind": "prefix", "value": "lark-cli "}]
    assert report["rejected_rules"] == [
        {"field": "shell", "value": _REDOS_POC, "reason": "unsafe_or_invalid_regex"}]


def test_full_rule_list_of_poc_patterns_stays_fast():
    """The per-command cost must not be multipliable by a long rule list."""
    from tasks import preauth
    rules = [{"kind": "regex", "value": _REDOS_POC}] * (preauth.MAX_RULES * 2)
    t0 = time.perf_counter()
    assert preauth.shell_match(rules, _REDOS_INPUT) is False
    assert time.perf_counter() - t0 < 0.1


@pytest.mark.parametrize("pattern", [
    r"^(a+)+$", r"(a*)*$", r"(a|a)+$", r"(?:\s+)+$", r"^(x+x+)+y",
    r"([a-z]+)+$", r"(\d+|\w+)*", r"(a+){2,}",
])
def test_catastrophic_shapes_are_detected(pattern):
    from tasks import preauth
    assert preauth._is_catastrophic_regex(pattern) is True


@pytest.mark.parametrize("pattern", [
    r"^gh (pr|issue) list", r"^lark-cli \S+", r"^git (status|log)$",
    r"^ls -la?$", r"(abc)+", r"^rsync -a /DATA/\w+ /DATA/backup$",
])
def test_benign_patterns_are_not_rejected(pattern):
    """The guard must not eat ordinary rules: an alternation group is fine as
    long as it is not itself quantified."""
    from tasks import preauth
    assert preauth._is_catastrophic_regex(pattern) is False
    assert preauth.parse({"shell": [{"kind": "regex", "value": pattern}]})["shell"]


def test_benign_regex_still_matches_after_the_guard():
    from tasks import preauth
    rules = preauth.parse({"shell": [
        {"kind": "regex", "value": r"^gh (pr|issue) list"}]})["shell"]
    assert preauth.shell_match(rules, "gh pr list --limit 5") is True


def test_uncompilable_regex_is_rejected_at_parse_time():
    from tasks import preauth
    doc, report = preauth.parse_with_report({"shell": [
        {"kind": "regex", "value": "("}]})
    assert doc["shell"] == []
    assert report["rejected_rules"][0]["reason"] == "unsafe_or_invalid_regex"


# ── fix round 2: M2 no over-refusal of everyday flags ────────────────────────

# `-c` / `-e` are everyday flags on ordinary tools. Round 1 refused every
# command carrying them, which broke the only use case this feature has.
_COMMON_FLAG_COMMANDS = [
    "sort -c f.txt",
    "cut -c 1-5 f.txt",
    "tar -c -f a.tar d",
    "git commit -c HEAD",
    "gcc -c a.c",
    "docker run -e X=1 img",
    "jq -e .a f.json",
    "ssh -e none host uptime",
    "openssl enc -e -in a -out b",
]


@pytest.mark.parametrize("cmd", _COMMON_FLAG_COMMANDS)
def test_run_allowlist_allows_common_flag_commands(monkeypatch, cmd):
    """M2: a run rule naming one of these must actually cover it."""
    _shell_setup(monkeypatch)
    decision = shell_guard.classify(cmd)
    assert decision.level != "protected", f"{cmd} — precondition changed"
    shell.RUN_ALLOWLIST_VAR.set([{"kind": "prefix", "value": cmd}])
    assert shell._run_allowlist_match(cmd, decision) is True


@pytest.mark.parametrize("cmd", ["curl -e https://ref https://x.com/a",
                                 "sed -e s/a/b/ f.txt"])
def test_curl_and_sed_blocked_by_classifier_not_by_flags(cmd):
    """These two stay refused, but NOT because of the flag: `classify` already
    calls them `protected` (a URL / a sed expression parsed as a path), and a
    protected command is never covered by a run grant. Documented so the cause
    isn't misattributed to the flag check — the control below carries no flag
    at all and is classified the same way."""
    assert shell_guard.classify(cmd).level == "protected"
    assert shell_guard.classify("curl -sS https://x.com/a").level == "protected"


def test_run_allowlist_var_default_is_immutable():
    """Minor5: a mutable default is shared by every context that never set the
    var — a single accidental in-place append would grant it to every run."""
    import contextvars

    def _read():
        return shell.RUN_ALLOWLIST_VAR.get()

    value = contextvars.Context().run(_read)   # empty context → declared default
    assert value == () and not isinstance(value, list)
