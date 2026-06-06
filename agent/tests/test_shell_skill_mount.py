from pathlib import Path
from skills.shell import _build_bwrap_opts, SANDBOX_SKILLS_VAR
from fs.sandbox_view import SandboxView


def test_skills_view_mounted_when_set(tmp_path):
    skills_view = tmp_path / "view"; skills_view.mkdir()
    token = SANDBOX_SKILLS_VAR.set(str(skills_view))
    try:
        args = _build_bwrap_opts(Path("/work"), SandboxView(), network=False)
    finally:
        SANDBOX_SKILLS_VAR.reset(token)
    assert "--ro-bind" in args
    assert str(skills_view) in args
    assert "/skill" in args


def test_default_is_unshare_net():
    args = _build_bwrap_opts(Path("/work"), SandboxView(), network=False)
    assert "--unshare-net" in args
    assert "--share-net" not in args


def test_network_true_shares_net():
    args = _build_bwrap_opts(Path("/work"), SandboxView(), network=True)
    assert "--share-net" in args


def test_authorized_binds_present():
    view = SandboxView(ro_binds=[("/data/proj", "/data/proj")])
    args = _build_bwrap_opts(Path("/work"), view, network=False)
    assert "--ro-bind" in args
    assert "/data/proj" in args
    assert "/bin/bash" not in args   # command is on the real argv, not in opts
