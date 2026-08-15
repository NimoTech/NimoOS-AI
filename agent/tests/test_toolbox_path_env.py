import importlib
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


def test_shell_argv_as_flag_reflects_mem_bytes(monkeypatch):
    """_shell_argv's prlimit --as flag must track whatever MEM_BYTES currently
    is, so an env-driven override (see test_mem_bytes_* below) actually takes
    effect on the real sandboxed invocation, not just on an unused constant.
    """
    from netns import executor
    monkeypatch.setattr(executor, "MEM_BYTES", 999_999)
    argv = executor._shell_argv("true")
    assert "--as=999999" in argv


def test_mem_bytes_default_is_2gib_when_env_unset(monkeypatch):
    """Regression for the prlimit --as=512M crash: Go 1.21+ runtimes fail
    outright in runtime.mallocinit() under a tight RLIMIT_AS (confirmed on
    118: 512M crashes `gh --version`; 1G/2G/4G all work). Default must be
    min(smallest-working-tier x2, 4G) = min(1G*2, 4G) = 2GiB, and must come
    from NIMOOS_SANDBOX_AS_BYTES so a box can raise it further without a
    code change.
    """
    monkeypatch.delenv("NIMOOS_SANDBOX_AS_BYTES", raising=False)
    from netns import executor
    importlib.reload(executor)
    try:
        assert executor.MEM_BYTES == 2 * 1024 * 1024 * 1024
    finally:
        importlib.reload(executor)  # restore module state for later tests


def test_mem_bytes_env_override(monkeypatch):
    monkeypatch.setenv("NIMOOS_SANDBOX_AS_BYTES", "4294967296")  # 4GiB
    from netns import executor
    importlib.reload(executor)
    try:
        assert executor.MEM_BYTES == 4 * 1024 * 1024 * 1024
    finally:
        monkeypatch.delenv("NIMOOS_SANDBOX_AS_BYTES", raising=False)
        importlib.reload(executor)  # restore module state for later tests
