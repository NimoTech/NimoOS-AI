"""Toolbox: persistent CLI components under /opt/toolbox (host bind mount).

Install runs in the agent process (direct network), NOT in the sandbox.
Self-contained tools only; system-lib-dependent tools must ship in the image.
"""
import asyncio, hashlib, json, os, platform, shutil, tempfile, time
from pathlib import Path

import httpx

_CATALOG_PATH = Path(__file__).with_name("catalog.json")
_DL_TIMEOUT = 600


class InstallError(Exception):
    pass


def toolbox_root() -> Path:
    return Path(os.environ.get("NIMOOS_TOOLBOX_ROOT", "/opt/toolbox"))


def load_catalog() -> list:
    return json.loads(_CATALOG_PATH.read_text("utf-8"))["components"]


def _arch() -> str:
    m = platform.machine()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(m, m)


async def _run(argv, env=None, cwd=None, timeout=600):
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env or dict(os.environ), cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise InstallError("install step timed out")
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _catalog_by_id(component_id: str) -> dict:
    for c in load_catalog():
        if c["id"] == component_id:
            return c
    raise InstallError(f"unknown component: {component_id}")


def _set_row(conn, cid, version, status, error=""):
    now = int(time.time())
    conn.execute(
        "INSERT INTO toolbox_components(id,version,status,error,installed_at,updated_at)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
        " version=excluded.version,status=excluded.status,error=excluded.error,"
        " updated_at=excluded.updated_at",
        (cid, version, status, error[:2000], now, now))
    conn.commit()


def _link_bins(prefix: Path, comp: dict):
    bin_dir = toolbox_root() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for b in comp["bins"]:
        name = Path(b).name
        target = prefix / "bin" / name
        if not target.exists():
            raise InstallError(f"expected binary missing after install: {target}")
        link = bin_dir / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(os.path.relpath(target, bin_dir))


async def _install_npm(comp: dict, prefix: Path):
    prefix.mkdir(parents=True, exist_ok=True)
    argv = ["npm", "install", "-g", f"{comp['npm_package']}@{comp['version']}",
            "--prefix", str(prefix)]
    code, out, err = await _run(argv, timeout=_DL_TIMEOUT)
    if code != 0:
        raise InstallError(f"npm failed: {err or out}")


async def _install_binary(comp: dict, prefix: Path):
    art = comp["artifacts"].get(_arch())
    if not art:
        raise InstallError(f"no artifact for arch {_arch()}")
    prefix.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tarball = Path(td) / "pkg.tar.gz"
        async with httpx.AsyncClient(timeout=_DL_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(art["url"])
            resp.raise_for_status()
            tarball.write_bytes(resp.content)
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if digest != art["sha256"]:
            raise InstallError(f"sha256 mismatch: got {digest}")
        code, out, err = await _run(["tar", "-xzf", str(tarball), "-C", td])
        if code != 0:
            raise InstallError(f"tar failed: {err}")
        src = Path(td) / art["bin_path"]
        dst = prefix / "bin" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        dst.chmod(0o755)


async def _self_check(comp: dict):
    # Deliberately NOT routed through _run(): _run is the seam tests
    # monkeypatch to stub out the network-bound install step (npm/tar), but
    # self-check must actually execute the binary that install just placed
    # under toolbox_root()/bin to prove it really works.
    env = dict(os.environ)
    env["PATH"] = f"{toolbox_root() / 'bin'}:{env.get('PATH', '')}"
    proc = await asyncio.create_subprocess_exec(
        *comp["check"], env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 30)
    except asyncio.TimeoutError:
        proc.kill()
        raise InstallError("self-check timed out")
    if proc.returncode != 0:
        raise InstallError(
            f"self-check failed: {err.decode(errors='replace') or out.decode(errors='replace')}")


async def install(conn, component_id: str) -> None:
    comp = _catalog_by_id(component_id)
    prefix = toolbox_root() / "pkgs" / comp["id"] / comp["version"]
    _set_row(conn, comp["id"], comp["version"], "installing")
    try:
        if comp["method"] == "npm":
            await _install_npm(comp, prefix)
        elif comp["method"] == "binary":
            await _install_binary(comp, prefix)
        else:
            raise InstallError(f"unknown method {comp['method']}")
        _link_bins(prefix, comp)
        await _self_check(comp)
    except Exception as e:
        _set_row(conn, comp["id"], comp["version"], "failed", str(e))
        raise InstallError(str(e)) from e
    _set_row(conn, comp["id"], comp["version"], "installed")


def uninstall(conn, component_id: str) -> None:
    comp = _catalog_by_id(component_id)
    for b in comp["bins"]:
        link = toolbox_root() / "bin" / Path(b).name
        if link.is_symlink() or link.exists():
            link.unlink()
    shutil.rmtree(toolbox_root() / "pkgs" / comp["id"], ignore_errors=True)
    conn.execute("DELETE FROM toolbox_components WHERE id=?", (component_id,))
    conn.commit()


def list_components(conn) -> list:
    rows = {r[0]: {"version": r[1], "status": r[2], "error": r[3]}
            for r in conn.execute("SELECT id,version,status,error FROM toolbox_components")}
    out = []
    managed_bins = set()
    for c in load_catalog():
        managed_bins.update(Path(b).name for b in c["bins"])
        row = rows.get(c["id"])
        out.append({
            "id": c["id"], "name": c["name"], "description": c["description"],
            "latest_version": c["version"],
            "installed_version": row["version"] if row and row["status"] == "installed" else None,
            "status": row["status"] if row else "not_installed",
            "error": row["error"] if row else "",
        })
    bin_dir = toolbox_root() / "bin"
    if bin_dir.is_dir():
        for p in sorted(bin_dir.iterdir()):
            if p.name not in managed_bins and os.access(p, os.X_OK):
                out.append({"id": f"unmanaged:{p.name}", "name": p.name,
                            "description": "Unmanaged binary (user-provided)",
                            "latest_version": "", "installed_version": "",
                            "status": "unmanaged", "error": ""})
    return out
