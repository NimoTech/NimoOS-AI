"""Regression tests for the SAFE-classification bypass family found in the
2026-07-16 adversarial security review of L1:

  1. exec-wrappers on the SAFE list (`env rm -rf /DATA` → SAFE)
  2. process substitution hidden inside a SAFE command (`cat <(rm -rf /DATA)`)
  3. env-assignment prefix masking the real verb (`X=1 rm -rf /DATA`)
  4. argv[0] basename trusted regardless of the actual binary (`/tmp/ls`)

The invariant under test: a destructive inner command must NEVER classify as
`safe`, no matter how it is wrapped, prefixed, or path-qualified.
"""
from shell_guard import classify


def _not_safe(cmd):
    lvl = classify(cmd).level
    assert lvl != "safe", f"{cmd!r} classified {lvl}, expected non-safe"
    return lvl


class TestExecWrapperBypass:
    def test_env_rm_escalates_to_protected(self):
        # env unwraps to `rm -rf /DATA` → mass delete under /DATA
        assert classify("env rm -rf /DATA").level == "protected"

    def test_env_absolute_path(self):
        assert classify("/usr/bin/env rm -rf /DATA").level == "protected"

    def test_env_dd_is_dangerous(self):
        _not_safe("env dd if=/dev/zero of=/dev/sda")

    def test_env_docker_rm_is_dangerous(self):
        _not_safe("env docker rm -f nimoos-core")

    def test_env_with_options_and_assignment(self):
        assert classify("env -i FOO=1 rm -rf /DATA").level == "protected"

    def test_nohup_wrapper_unwrapped(self):
        assert classify("nohup rm -rf /DATA").level == "protected"

    def test_timeout_skips_duration_operand(self):
        assert classify("timeout 5 rm -rf /DATA").level == "protected"

    def test_sudo_wrapper_unwrapped(self):
        _not_safe("sudo rm -rf /DATA")


class TestEnvAssignmentPrefix:
    def test_assignment_prefix_reveals_rm(self):
        assert classify("X=1 rm -rf /DATA").level == "protected"

    def test_multiple_assignments(self):
        _not_safe("A=1 B=2 dd if=/dev/zero of=/dev/sda")


class TestProcessSubstitution:
    def test_process_sub_forces_gray(self):
        assert classify("cat <(rm -rf /DATA)").level == "gray"

    def test_process_sub_grep(self):
        assert classify("grep x <(rm -rf /DATA)").level == "gray"

    def test_output_process_sub_forces_gray(self):
        assert classify("tee >(rm -rf /DATA)").level == "gray"


class TestUntrustedArgv0:
    def test_dropped_binary_named_ls_not_safe(self):
        # a binary at /tmp/ls is not the system ls — must not be trusted SAFE
        assert classify("/tmp/ls -la /DATA").level == "gray"

    def test_relative_path_binary_not_safe(self):
        assert classify("./ls").level == "gray"

    def test_standard_bin_path_still_safe(self):
        assert classify("/bin/ls").level == "safe"

    def test_bare_name_still_safe(self):
        assert classify("ls -la /DATA").level == "safe"

    def test_bare_cat_still_safe(self):
        assert classify("cat /DATA/notes.txt").level == "safe"
