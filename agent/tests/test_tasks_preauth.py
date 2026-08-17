"""Run-scoped pre-authorization (scheduled tasks M2, Task 2).

Covers the pure rule matcher (`tasks.preauth`) and the two gates it feeds:
the shell guard's run-scoped allowlist and the MCP per-server wildcard
pre-confirmation.  The security-critical assertions are the NEGATIVE ones:
a run-scoped grant must never widen the gate beyond a single simple command,
and must never cover a `protected`-level command.
"""
import asyncio

import pytest

import db as dbmod
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
    shell.RUN_ALLOWLIST_VAR.set([])


def _shell_setup(monkeypatch, mgr=None, sink=None, unattended=True):
    conn = dbmod.init_db(":memory:")
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) "
                 "VALUES ('s1','u1',0,0)")
    conn.commit()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)
    shell.CONFIRM_MGR_VAR.set(None if unattended else mgr)
    shell.EVENT_QUEUE_VAR.set(None if unattended else sink)
    shell.RUN_ALLOWLIST_VAR.set([])
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

    assert shell.RUN_ALLOWLIST_VAR.get() == []
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
