"""Read/write split for protected paths (2026-08-21) + /dev/null exemption.

INTEGRITY-class prefixes (/usr, /boot, /opt/nimoos) downgrade to GRAY when a
positively read-only trusted command touches them; SECRET-class paths (/etc,
/var/lib/nimoos, *.key/*.pem, /.ssh/, agent.db) stay PROTECTED for any access.
"""
from shell_guard import classify


AGENT_SRC = "/usr/share/nimoos/agent/main.py"


# ── integrity-class reads downgrade to gray ─────────────────────────────────

def test_grep_of_agent_source_is_gray_not_protected():
    d = classify(f"grep -rn foo {AGENT_SRC}")
    assert d.level == "gray"
    assert "read-only" in d.reason


def test_cat_and_sed_n_of_usr_are_gray():
    assert classify(f"cat {AGENT_SRC}").level == "gray"
    assert classify(f"sed -n 1,40p {AGENT_SRC}").level == "gray"


def test_pipeline_with_dev_null_stays_gray():
    # The exact shape from the live box that motivated the change.
    d = classify(f'grep -rn "M5" {AGENT_SRC} 2>/dev/null | head -40')
    assert d.level == "gray"


def test_read_redirect_of_integrity_path_is_gray():
    assert classify(f"head < {AGENT_SRC}").level == "gray"


def test_find_without_mutating_operands_is_gray():
    assert classify("find /usr/share/nimoos -name '*.py'").level == "gray"


# ── everything below must NOT downgrade ─────────────────────────────────────

def test_secret_paths_stay_protected_on_read():
    assert classify("cat /etc/shadow").level == "protected"
    assert classify("grep x /var/lib/nimoos/db/user.db").level == "protected"
    assert classify("cat /usr/share/nimoos/agent/agent.db").level == "protected"
    assert classify("head /root/.ssh/id_rsa").level == "protected"
    assert classify("cat /home/nimo/server.pem").level == "protected"
    assert classify("strings /home/nimo/x.key").level == "protected"


def test_write_redirect_into_integrity_path_stays_protected():
    assert classify(f"echo pwned > {AGENT_SRC}").level == "protected"
    assert classify(f"sort /tmp/a > {AGENT_SRC}").level == "protected"


def test_writing_verbs_on_integrity_paths_stay_protected():
    assert classify(f"rm {AGENT_SRC}").level == "protected"
    assert classify(f"cp /tmp/evil.py {AGENT_SRC}").level == "protected"
    assert classify(f"mv {AGENT_SRC} /tmp/x").level == "protected"
    assert classify(f"tee {AGENT_SRC}").level == "protected"
    assert classify(f"python3 {AGENT_SRC}").level == "protected"


def test_sed_in_place_on_integrity_path_stays_protected():
    assert classify(f"sed -i s/a/b/ {AGENT_SRC}").level == "protected"
    assert classify(f"sed -ni s/a/b/p {AGENT_SRC}").level == "protected"
    assert classify(f"sed --in-place s/a/b/ {AGENT_SRC}").level == "protected"


def test_find_with_mutating_operands_stays_protected():
    assert classify("find /usr/share/nimoos -delete").level == "protected"
    assert classify("find /usr/share/nimoos -exec rm {} ;").level == "protected"


def test_untrusted_argv0_never_downgrades():
    assert classify(f"/tmp/grep foo {AGENT_SRC}").level == "protected"
    assert classify(f"./cat {AGENT_SRC}").level == "protected"


def test_wrapper_unwrap_still_applies():
    # env unwraps to cat → read-only downgrade applies through the wrapper
    assert classify(f"env cat {AGENT_SRC}").level == "gray"
    # env unwraps to rm → protected as before
    assert classify(f"env rm {AGENT_SRC}").level == "protected"


# ── /dev/null exemption ──────────────────────────────────────────────────────

def test_dev_null_redirect_is_not_dangerous():
    assert classify("ls 2>/dev/null").level == "safe"
    assert classify("grep -r foo /DATA/docs 2>/dev/null").level == "safe"


def test_real_device_redirect_is_still_dangerous():
    assert classify("echo x > /dev/sda").level == "dangerous"


def test_write_redirect_to_file_still_not_safe():
    assert classify("ls > /tmp/out.txt").level != "safe"
