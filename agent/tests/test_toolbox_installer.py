import asyncio, json, os, stat, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import pytest
import db as db_module


@pytest.fixture
def conn(tmp_path):
    return db_module.init_db(str(tmp_path / "t.db"))


@pytest.fixture
def root(tmp_path, monkeypatch):
    from toolbox import installer
    r = tmp_path / "toolbox"
    monkeypatch.setenv("NIMOOS_TOOLBOX_ROOT", str(r))
    return r


def test_load_catalog_has_lark_and_gh():
    from toolbox import installer
    ids = {c["id"] for c in installer.load_catalog()}
    assert {"lark-cli", "gh"} <= ids


def test_list_components_reports_not_installed(conn, root):
    from toolbox import installer
    comps = {c["id"]: c for c in installer.list_components(conn)}
    assert comps["gh"]["installed_version"] is None
    assert comps["gh"]["status"] == "not_installed"


def test_npm_install_creates_symlink_and_row(conn, root, monkeypatch):
    from toolbox import installer

    async def fake_run(argv, env=None, cwd=None, timeout=600):
        # 模拟 npm:在 prefix/bin 下产出可执行文件
        prefix = pathlib.Path(argv[argv.index("--prefix") + 1])
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        exe = prefix / "bin" / "lark-cli"
        exe.write_text("#!/bin/sh\necho lark-cli version 1.0.85\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return 0, "ok", ""

    monkeypatch.setattr(installer, "_run", fake_run)
    asyncio.run(installer.install(conn, "lark-cli"))
    link = root / "bin" / "lark-cli"
    assert link.is_symlink() and os.access(link, os.X_OK)
    row = conn.execute("SELECT version,status FROM toolbox_components WHERE id='lark-cli'").fetchone()
    # db_module.init_db() sets conn.row_factory = sqlite3.Row, and sqlite3.Row
    # does not compare equal to a plain tuple (unlike the default row type) —
    # cast explicitly rather than comparing the Row object directly.
    assert tuple(row) == ("1.0.85", "installed")


def test_install_failure_records_error(conn, root, monkeypatch):
    from toolbox import installer

    async def fake_run(argv, env=None, cwd=None, timeout=600):
        return 1, "", "npm ERR! boom"

    monkeypatch.setattr(installer, "_run", fake_run)
    with pytest.raises(installer.InstallError):
        asyncio.run(installer.install(conn, "lark-cli"))
    row = conn.execute("SELECT status,error FROM toolbox_components WHERE id='lark-cli'").fetchone()
    assert row[0] == "failed" and "boom" in row[1]


def test_uninstall_removes_symlink_and_row(conn, root, monkeypatch):
    from toolbox import installer

    async def fake_run(argv, env=None, cwd=None, timeout=600):
        prefix = pathlib.Path(argv[argv.index("--prefix") + 1])
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        exe = prefix / "bin" / "lark-cli"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        return 0, "", ""
    monkeypatch.setattr(installer, "_run", fake_run)
    asyncio.run(installer.install(conn, "lark-cli"))
    installer.uninstall(conn, "lark-cli")
    assert not (root / "bin" / "lark-cli").exists()
    assert conn.execute("SELECT COUNT(*) FROM toolbox_components").fetchone()[0] == 0


def test_unmanaged_bin_listed(conn, root):
    from toolbox import installer
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "mytool"
    exe.write_text("#!/bin/sh\n"); exe.chmod(0o755)
    ids = {c["id"] for c in installer.list_components(conn)}
    assert "unmanaged:mytool" in ids
