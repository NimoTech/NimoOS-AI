import os
import pytest
from fs import ignore as ig


def test_implicit_blocks_dot_git(tmp_path):
    p = tmp_path / "repo" / ".git" / "config"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    visible_roots = [str(tmp_path / "repo")]
    with pytest.raises(ig.BlockedImplicit):
        ig.gate(str(p), visible_roots, user_patterns=[])


def test_hard_builtin_blocks_ssh(tmp_path):
    base = tmp_path / "home" / ".ssh"
    base.mkdir(parents=True)
    p = base / "id_rsa"
    p.write_text("k")
    with pytest.raises(ig.BlockedHardBlacklist):
        ig.gate(str(p), [str(tmp_path / "home")], user_patterns=[])


def test_user_pattern_blocks(tmp_path):
    p = tmp_path / "repo" / "secret.bak"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    with pytest.raises(ig.BlockedHardBlacklist):
        ig.gate(str(p), [str(tmp_path / "repo")], user_patterns=["*.bak"])


def test_gitignore_blocks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("logs/\n")
    (repo / "logs").mkdir()
    (repo / "logs" / "x.log").write_text("a")
    with pytest.raises(ig.BlockedGitignore):
        ig.gate(str(repo / "logs" / "x.log"), [str(repo)], user_patterns=[])


def test_gitignore_allows_when_force_override(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / ".gitignore").write_text("logs/\n")
    (repo / "logs").mkdir()
    (repo / "logs" / "x.log").write_text("a")
    # Should not raise
    ig.gate(str(repo / "logs" / "x.log"), [str(repo)],
            user_patterns=[], allow_gitignore_override=True)


def test_gitignore_force_does_not_break_implicit(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    p = repo / ".git" / "head"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    with pytest.raises(ig.BlockedImplicit):
        ig.gate(str(p), [str(repo)], user_patterns=[],
                allow_gitignore_override=True)


def test_nested_gitignore_negation(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / ".gitignore").write_text("*.log\n")
    sub = repo / "sub"; sub.mkdir()
    (sub / ".gitignore").write_text("!keep.log\n")
    target = sub / "keep.log"
    target.write_text("ok")
    # negation in nested .gitignore should re-include
    ig.gate(str(target), [str(repo)], user_patterns=[])
