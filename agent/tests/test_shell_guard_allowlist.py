import sqlite3
import db as dbmod
from shell_guard import allowlist as AL


def _db():
    conn = dbmod.init_db(":memory:")
    return conn


def test_prefix_match():
    conn = _db()
    AL.add(conn, "prefix", "git pull", "user")
    assert AL.match(conn, "git pull origin main") is True
    assert AL.match(conn, "git push") is False


def test_regex_match():
    conn = _db()
    AL.add(conn, "regex", r"^rsync -a /DATA/ /backup/", "user")
    assert AL.match(conn, "rsync -a /DATA/ /backup/ --delete") is True
    assert AL.match(conn, "rm -rf /DATA") is False


def test_prefix_does_not_match_newline_smuggled_tail():
    """A newline is a bash command separator, so `match()` (single-segment only)
    must NOT vouch for `git pull\\nrm -rf /DATA` on a `git pull` prefix entry."""
    conn = _db()
    AL.add(conn, "prefix", "git pull", "user")
    assert AL.match(conn, "git pull\nrm -rf /DATA") is False


def test_anchored_exact_regex_rejects_superset():
    """I2: 'remember' stores an anchored exact-match regex, not an open prefix,
    so a superset command touching an unapproved extra path does NOT match."""
    conn = _db()
    import re
    AL.add(conn, "regex", f"^{re.escape('rm -rf /DATA/scratch')}$", "confirm-card")
    assert AL.match(conn, "rm -rf /DATA/scratch") is True
    assert AL.match(conn, "rm -rf /DATA/scratch /DATA/important") is False


def test_path_scope_match():
    conn = _db()
    AL.add(conn, "path_scope", "/DATA/scratch", "user")
    assert AL.match(conn, "rm -rf /DATA/scratch/tmp") is True
    assert AL.match(conn, "rm -rf /DATA/important") is False


def test_list_and_delete():
    conn = _db()
    eid = AL.add(conn, "prefix", "ls", "user", note="harmless")
    rows = AL.list_entries(conn)
    assert len(rows) == 1 and rows[0]["value"] == "ls"
    assert AL.delete(conn, eid) is True
    assert AL.list_entries(conn) == []


def test_bad_regex_never_matches_never_raises():
    conn = _db()
    AL.add(conn, "regex", "([", "user")  # invalid regex
    assert AL.match(conn, "anything") is False


def test_prefix_no_chaining_or_redirect_bypass():
    conn = _db()
    AL.add(conn, "prefix", "git pull", "user")
    # A benign allowed prefix must NOT vouch for smuggled extra operations.
    assert AL.match(conn, "git pull; rm -rf /DATA") is False
    assert AL.match(conn, "git pull && rm -rf /x") is False
    assert AL.match(conn, "git pull > /etc/passwd") is False


def test_path_scope_requires_all_paths_in_scope():
    conn = _db()
    AL.add(conn, "path_scope", "/DATA/scratch", "user")
    # Every path target must be in scope; one out-of-scope path fails closed.
    assert AL.match(conn, "rm -rf /DATA/scratch/a /DATA/important") is False


def test_unparseable_command_fails_closed():
    conn = _db()
    AL.add(conn, "prefix", "echo", "user")
    AL.add(conn, "path_scope", "/DATA/scratch", "user")
    # Command substitution / subshells are unparseable → never vouched.
    assert AL.match(conn, "echo $(rm -rf /DATA)") is False
