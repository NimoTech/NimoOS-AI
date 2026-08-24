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


# --- offline deps mirror + upgrade (spec §7 离线 / §9 upgrade) ---------------

def _mk_deps(root, comp_id, version):
    d = root / "deps" / comp_id / version
    d.mkdir(parents=True)
    return d


def _fake_npm_run(argv, env=None, cwd=None, timeout=600):
    prefix = pathlib.Path(argv[argv.index("--prefix") + 1])
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    exe = prefix / "bin" / "lark-cli"
    exe.write_text("#!/bin/sh\necho lark-cli version 1.0.85\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return 0, "ok", ""


def test_npm_install_prefers_local_tgz(conn, root, monkeypatch):
    from toolbox import installer
    tgz = _mk_deps(root, "lark-cli", "1.0.85") / "larksuite-cli-1.0.85.tgz"
    tgz.write_bytes(b"fake-tarball")
    calls = []

    async def fake_run(argv, env=None, cwd=None, timeout=600):
        calls.append(list(argv))
        return _fake_npm_run(argv)

    monkeypatch.setattr(installer, "_run", fake_run)
    asyncio.run(installer.install(conn, "lark-cli"))
    # npm must be pointed at the LOCAL tarball, not the registry coordinate
    assert any(str(tgz) in a for a in calls[0])
    assert not any("@larksuite/cli@" in a for a in calls[0])


def test_binary_install_prefers_local_tarball_offline(conn, root, monkeypatch):
    from toolbox import installer
    import hashlib, io, tarfile
    comp = installer._catalog_by_id("gh")
    art = comp["artifacts"][installer._arch()]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"#!/bin/sh\necho gh version x\n"
        info = tarfile.TarInfo(art["bin_path"])
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    d = _mk_deps(root, "gh", comp["version"])
    (d / art["url"].rsplit("/", 1)[-1]).write_bytes(blob)
    patched = {**comp, "artifacts": {installer._arch():
               {**art, "sha256": hashlib.sha256(blob).hexdigest()}}}
    monkeypatch.setattr(installer, "_catalog_by_id", lambda cid: patched)

    class Boom:  # any network use is a test failure
        def __init__(self, *a, **k):
            raise AssertionError("network used in offline install")
    monkeypatch.setattr(installer.httpx, "AsyncClient", Boom)
    asyncio.run(installer.install(conn, "gh"))
    assert (root / "pkgs" / "gh" / comp["version"] / "bin" / "gh").exists()
    row = conn.execute("SELECT status FROM toolbox_components WHERE id='gh'").fetchone()
    assert row[0] == "installed"


def test_binary_local_sha_mismatch_fails(conn, root, monkeypatch):
    from toolbox import installer
    comp = installer._catalog_by_id("gh")
    art = comp["artifacts"][installer._arch()]
    d = _mk_deps(root, "gh", comp["version"])
    (d / art["url"].rsplit("/", 1)[-1]).write_bytes(b"tampered")

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("network used")
    monkeypatch.setattr(installer.httpx, "AsyncClient", Boom)
    with pytest.raises(installer.InstallError):
        asyncio.run(installer.install(conn, "gh"))
    row = conn.execute("SELECT status FROM toolbox_components WHERE id='gh'").fetchone()
    assert row[0] == "failed"


def test_install_prunes_stale_version_dirs(conn, root, monkeypatch):
    from toolbox import installer
    stale = root / "pkgs" / "lark-cli" / "0.9.0" / "bin"
    stale.mkdir(parents=True)
    (stale / "lark-cli").write_text("old")

    async def fake_run(argv, env=None, cwd=None, timeout=600):
        return _fake_npm_run(argv)

    monkeypatch.setattr(installer, "_run", fake_run)
    asyncio.run(installer.install(conn, "lark-cli"))
    assert not (root / "pkgs" / "lark-cli" / "0.9.0").exists()
    assert (root / "pkgs" / "lark-cli" / "1.0.85" / "bin" / "lark-cli").exists()
    # the shared symlink points at the surviving version
    assert (root / "bin" / "lark-cli").resolve() == \
        (root / "pkgs" / "lark-cli" / "1.0.85" / "bin" / "lark-cli").resolve()


def test_failed_install_keeps_previous_version(conn, root, monkeypatch):
    from toolbox import installer
    prev = root / "pkgs" / "lark-cli" / "0.9.0" / "bin"
    prev.mkdir(parents=True)
    exe = prev / "lark-cli"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "lark-cli").symlink_to(
        os.path.relpath(exe, root / "bin"))

    async def fake_run(argv, env=None, cwd=None, timeout=600):
        return 1, "", "npm ERR! offline"

    monkeypatch.setattr(installer, "_run", fake_run)
    with pytest.raises(installer.InstallError):
        asyncio.run(installer.install(conn, "lark-cli"))
    # previous version dir AND its symlink survive a failed upgrade
    assert exe.exists()
    assert (root / "bin" / "lark-cli").resolve() == exe.resolve()


def test_upgrade_is_install_plus_prune(conn, root, monkeypatch):
    from toolbox import installer

    async def fake_run(argv, env=None, cwd=None, timeout=600):
        return _fake_npm_run(argv)

    monkeypatch.setattr(installer, "_run", fake_run)
    stale = root / "pkgs" / "lark-cli" / "0.8.0"
    stale.mkdir(parents=True)
    asyncio.run(installer.upgrade(conn, "lark-cli"))
    row = conn.execute(
        "SELECT version,status FROM toolbox_components WHERE id='lark-cli'").fetchone()
    assert tuple(row) == ("1.0.85", "installed")
    assert not stale.exists()


def test_deps_dir_env_override(monkeypatch, tmp_path):
    from toolbox import installer
    monkeypatch.setenv("NIMOOS_TOOLBOX_DEPS_DIR", str(tmp_path / "elsewhere"))
    assert installer.deps_dir() == tmp_path / "elsewhere"
