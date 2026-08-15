import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_sandbox_path_prepends_toolbox_when_dir_exists(monkeypatch, tmp_path):
    from netns import executor
    monkeypatch.setattr(executor, "TOOLBOX_BIN", str(tmp_path))
    assert executor._sandbox_path() == f"{tmp_path}:/usr/bin:/usr/sbin:/bin:/sbin"


def test_sandbox_path_plain_when_toolbox_missing(monkeypatch, tmp_path):
    from netns import executor
    monkeypatch.setattr(executor, "TOOLBOX_BIN", str(tmp_path / "nope"))
    assert executor._sandbox_path() == "/usr/bin:/usr/sbin:/bin:/sbin"


def test_user_home_env_when_uid_and_root_exist(monkeypatch, tmp_path):
    from skills import shell, skills_registry
    monkeypatch.setattr(shell, "HOMES_ROOT", tmp_path)
    tok = skills_registry.USER_ID_VAR.set("u1")
    try:
        env = shell._user_home_env()
        assert env == {"HOME": str(tmp_path / "u1")}
        assert (tmp_path / "u1").is_dir()  # 惰性创建
    finally:
        skills_registry.USER_ID_VAR.reset(tok)


def test_user_home_env_empty_without_uid(monkeypatch, tmp_path):
    from skills import shell, skills_registry
    monkeypatch.setattr(shell, "HOMES_ROOT", tmp_path)
    tok = skills_registry.USER_ID_VAR.set("")
    try:
        assert shell._user_home_env() == {}
    finally:
        skills_registry.USER_ID_VAR.reset(tok)


def test_shell_argv_reexports_sandbox_path_past_login_shell_profile(monkeypatch, tmp_path):
    """Regression for the /etc/profile PATH stomp under `bash -lc` (login shell):
    /etc/profile unconditionally re-exports PATH, discarding whatever PATH was
    passed via env=... to Popen. _shell_argv must therefore re-assert the
    sandbox PATH *inside* the command string itself (an `export PATH=...;`
    prefix), not just via the environment.
    """
    from netns import executor
    monkeypatch.setattr(executor, "TOOLBOX_BIN", str(tmp_path))
    argv = executor._shell_argv("gh --version")

    assert argv[-3:-1] == ["/bin/bash", "-lc"]
    bash_cmd = argv[-1]
    assert bash_cmd.startswith("export PATH=")
    assert str(tmp_path) in bash_cmd  # sandbox PATH (incl. toolbox dir) is asserted
    assert bash_cmd.endswith("gh --version")  # original command preserved verbatim


def test_shell_argv_plain_path_when_toolbox_missing(monkeypatch, tmp_path):
    from netns import executor
    monkeypatch.setattr(executor, "TOOLBOX_BIN", str(tmp_path / "nope"))
    argv = executor._shell_argv("echo hi")
    bash_cmd = argv[-1]
    assert bash_cmd.startswith("export PATH=")
    assert "/usr/bin:/usr/sbin:/bin:/sbin" in bash_cmd
    assert bash_cmd.endswith("echo hi")
