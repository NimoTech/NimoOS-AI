"""Regression tests for the minor-tier follow-ups (M2–M6) from the 2026-07-16
guardrail review — see docs/superpowers/specs/2026-07-16-agent-guardrails-followups.md.
"""
import shell_guard.backstop as B
from shell_guard import classify
from shell_guard.parse import segments


# ── M3: ${IFS} / $IFS word-split obfuscation ─────────────────────────────────
class TestIfsExpansion:
    def test_braced_ifs_recovers_real_argv(self):
        assert classify("rm${IFS}-rf${IFS}/DATA").level == "protected"

    def test_bare_ifs_recovers_real_argv(self):
        assert classify("rm$IFS-rf$IFS/DATA").level == "protected"

    def test_ifs_does_not_break_normal_command(self):
        # a quoted ${IFS} is left alone (not our attack surface)
        assert segments('echo "${IFS}"') is not None

    def test_ifs_wrapped_dd(self):
        assert classify("dd${IFS}if=/dev/zero${IFS}of=/dev/sda").level != "safe"


# ── M2: relative paths resolve against the execution cwd, not process cwd ─────
    def test_relative_path_resolves_against_given_cwd(self):
        # 'sub/f' under cwd=/etc → /etc/sub/f → PROTECTED
        assert classify("cat sub/f", cwd="/etc").level == "protected"

    def test_same_relative_path_benign_under_data_cwd(self):
        # identical token, different cwd → not protected
        d = classify("cat sub/f", cwd="/DATA/session/work")
        assert d.level != "protected"

    def test_dotdot_escape_resolved_with_cwd(self):
        # /DATA/a/b + ../../../etc/shadow → /etc/shadow (PROTECTED)
        assert classify("cat ../../../etc/shadow", cwd="/DATA/a/b").level == "protected"


# ── M6: btrfs snapshot shortcut only claims undoable for a single target ──────
class TestBtrfsMultiTarget:
    def test_single_btrfs_target_uses_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(B, "fs_type", lambda p: "btrfs")
        calls = []
        monkeypatch.setattr(B, "_snapshot_btrfs",
                            lambda t, r: calls.append(t) or B.BackstopResult(
                                "snapshot", "/x", True, "stub"))
        one = tmp_path / "a"; one.mkdir()
        res = B.prepare_backstop([str(one)], trash_root=str(tmp_path / "trash"))
        assert res.kind == "snapshot" and calls == [str(one)]

    def test_multiple_btrfs_targets_do_not_use_single_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(B, "fs_type", lambda p: "btrfs")
        calls = []
        monkeypatch.setattr(B, "_snapshot_btrfs",
                            lambda t, r: calls.append(t) or B.BackstopResult(
                                "snapshot", "/x", True, "stub"))
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        res = B.prepare_backstop([str(a), str(b)], trash_root=str(tmp_path / "trash"))
        # must NOT take the one-subvolume snapshot (would over-claim undoable)
        assert calls == []
        assert res.kind in ("trash", "none")
