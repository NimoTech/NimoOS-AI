import sys, pathlib, asyncio
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import main
from toolbox import installer

H = {"X-User-Id": "u1"}


def _client():
    # No `with` block: entering TestClient as a context manager runs FastAPI
    # lifespan (startup/shutdown), which re-runs the MCP StreamableHTTP
    # session manager's .run() — that singleton errors if started more than
    # once per process. Every other endpoint test file in this repo (e.g.
    # tests/test_shell_allowlist_endpoints.py) avoids the lifespan for the
    # same reason; requests still route through the app either way.
    return TestClient(main.app)


def test_list_components():
    c = _client()
    r = c.get("/agent/toolbox", headers=H)
    assert r.status_code == 200
    ids = {x["id"] for x in r.json()["components"]}
    assert {"lark-cli", "gh"} <= ids


def test_install_dispatches_background(monkeypatch):
    done = asyncio.Event()

    async def fake_install(conn, cid):
        done.set()

    monkeypatch.setattr(installer, "install", fake_install)
    c = _client()
    r = c.post("/agent/toolbox/install", headers=H, json={"id": "gh"})
    assert r.status_code == 202


def test_install_unknown_404():
    c = _client()
    r = c.post("/agent/toolbox/install", headers=H, json={"id": "nope"})
    assert r.status_code == 404


def test_upgrade_dispatches_background(monkeypatch):
    called = []

    async def fake_upgrade(conn, cid):
        called.append(cid)

    monkeypatch.setattr(installer, "upgrade", fake_upgrade)
    c = _client()
    r = c.post("/agent/toolbox/upgrade", headers=H, json={"id": "gh"})
    assert r.status_code == 202
    assert r.json()["status"] == "upgrading"


def test_upgrade_unknown_404():
    c = _client()
    r = c.post("/agent/toolbox/upgrade", headers=H, json={"id": "nope"})
    assert r.status_code == 404


def test_upgrade_conflicts_with_running_install():
    class RunningJob:
        def done(self):
            return False

    c = _client()
    # Occupy the per-component job slot the way a running install would.
    # Deliberately the module-level `main` this file imported at collection
    # time — the same object _client() routes through. A fresh `import main`
    # here can resolve a DIFFERENT module under the full suite, because
    # test_main_agent_type.py's fixture pops "main" from sys.modules and
    # re-imports it; writing the job into that copy leaves the app's own
    # _TOOLBOX_JOBS empty and the 409 never fires.
    main._TOOLBOX_JOBS["gh"] = RunningJob()
    try:
        r = c.post("/agent/toolbox/upgrade", headers=H, json={"id": "gh"})
        assert r.status_code == 409
    finally:
        main._TOOLBOX_JOBS.pop("gh", None)
