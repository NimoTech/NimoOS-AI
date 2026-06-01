"""End-to-end test: real nimoos-wiki binary + WikiClient + WikiContextBuilder.

# What this tests
Register a root directory → wait for wiki node creation → append user notes →
read them back via WikiClient → verify WikiContextBuilder.build() includes the
registered path.  This exercises the full Python→HTTP→Go→SQLite→HTTP→Python
round-trip without any mocks.

# Skip conditions (loudly explained)
The test auto-skips unless ALL of the following are true:
  1. A Go toolchain (``go`` binary) is on PATH, OR NIMOOS_WIKI_BIN is set to
     an already-built binary.
  2. The NimoOS-Wiki source tree exists at NIMOOS_WIKI_SRC
     (default: /home/nimo/nimoos/NimoOS-Wiki), OR NIMOOS_WIKI_BIN is set.
  3. The wiki binary can be built (or NIMOOS_WIKI_BIN was provided pre-built).
  4. A free TCP port is available on 127.0.0.1.

# Why wiki requires a "fake gateway"
nimoos-wiki's main.go unconditionally calls
``external.NewManagementService(runtimePath)`` before accepting any traffic.
That function reads ``{RuntimePath}/management.url``, then POSTs
``POST /v1/gateway/routes`` to register its routes with the API gateway.  If
the file is absent it retries for 10 s then panics; if the file is present but
the server is unreachable the binary panics immediately.  There is no config
key, env var, or CLI flag to skip gateway registration.

The fixture therefore spins up a minimal ``http.server``-like stub (pure Python,
``http.server.BaseHTTPRequestHandler``) that:
  - answers ``GET /ping`` → 200 (gateway health check)
  - answers ``POST /v1/gateway/routes`` → 201 (route registration)
  - ignores everything else → 200

The stub runs in a daemon thread; its URL is written to
``{RuntimePath}/management.url`` before the wiki binary is launched.

# Manual smoke test (for when you can't build)
If you have a running NimoOS instance:
  1. Find the wiki URL: ``cat /var/run/nimoos/wiki.url``
  2. Register a root:
       curl -s -X POST http://<url>/v1/wiki/roots \
            -H 'X-NimoOS-User-ID: 1' \
            -H 'Content-Type: application/json' \
            -d '{"path":"/tmp/smoke","level":"project"}'
  3. Wait 2 s, then read the node:
       curl -s 'http://<url>/v1/wiki/node?path=/tmp/smoke' \
            -H 'X-NimoOS-User-ID: 1'
  4. Run the Python agent smoke_test.sh to exercise WikiContextBuilder.
"""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest

# ---------------------------------------------------------------------------
# Environment knobs
# ---------------------------------------------------------------------------

WIKI_SRC = os.environ.get("NIMOOS_WIKI_SRC", "/home/nimo/nimoos/NimoOS-Wiki")
WIKI_BIN_ENV = os.environ.get("NIMOOS_WIKI_BIN", "")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Return a free TCP port on 127.0.0.1 (released before returning)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 15.0) -> bool:
    """Poll ``url`` with GET until 200 or timeout.  Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Fake gateway stub
# ---------------------------------------------------------------------------


class _GatewayStubHandler(http.server.BaseHTTPRequestHandler):
    """Minimal stub that satisfies wiki's gateway registration protocol."""

    def log_message(self, fmt: str, *args: object) -> None:  # silence access log
        pass

    def _ok(self, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        # ``external.ping()`` calls ``GET {management_url}/ping``
        self._ok(200)

    def do_POST(self) -> None:  # noqa: N802
        # ``external.CreateRoute()`` expects 201
        body_len = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(body_len)  # drain body
        self._ok(201)


def _start_gateway_stub() -> str:
    """Start the fake gateway on a random port.  Returns its base URL."""
    port = _free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), _GatewayStubHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wiki_binary(tmp_path_factory) -> str:
    """Return path to a nimoos-wiki binary, building it if necessary."""
    if WIKI_BIN_ENV:
        if not Path(WIKI_BIN_ENV).is_file():
            pytest.skip(f"NIMOOS_WIKI_BIN={WIKI_BIN_ENV!r} not found")
        return WIKI_BIN_ENV

    # Need source tree
    if not Path(WIKI_SRC).exists():
        pytest.skip(
            f"NimoOS-Wiki source not found at {WIKI_SRC!r}. "
            "Set NIMOOS_WIKI_SRC to its location, or set NIMOOS_WIKI_BIN to a "
            "pre-built binary."
        )

    # Need Go toolchain
    if not shutil.which("go"):
        pytest.skip(
            "No 'go' binary on PATH — cannot build nimoos-wiki. "
            "Install Go ≥1.21 or set NIMOOS_WIKI_BIN to a pre-built binary."
        )

    out_dir = tmp_path_factory.mktemp("wiki-bin")
    out = out_dir / "nimoos-wiki"
    res = subprocess.run(
        ["go", "build", "-o", str(out), "./"],
        cwd=WIKI_SRC,
        capture_output=True,
        timeout=300,
    )
    if res.returncode != 0:
        pytest.skip(
            "nimoos-wiki build failed (returncode={}):\n{}".format(
                res.returncode,
                res.stderr.decode(errors="replace")[:800],
            )
        )
    return str(out)


@pytest.fixture
def running_wiki(wiki_binary, tmp_path):
    """
    Start a nimoos-wiki process against tmp dirs with a fake gateway stub.

    Layout created in tmp_path:
      conf/wiki.conf    — custom config pointing at tmp dirs
      data/             — DataPath (wiki.db lives here)
      run/              — RuntimePath (wiki.url, management.url written here)
      log/              — LogPath

    The fake gateway stub is started first; its URL is written to
    run/management.url so wiki's gateway-registration step succeeds.

    Yields the wiki's base URL (e.g. ``http://127.0.0.1:54321``).
    """
    conf_dir = tmp_path / "conf"
    data_dir = tmp_path / "data"
    run_dir = tmp_path / "run"
    log_dir = tmp_path / "log"
    for d in (conf_dir, data_dir, run_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Start fake gateway; write its URL to management.url
    gw_url = _start_gateway_stub()
    (run_dir / "management.url").write_text(gw_url)

    # Write config file
    conf_path = conf_dir / "wiki.conf"
    conf_path.write_text(
        textwrap.dedent(f"""\
            [common]
            RuntimePath = {run_dir}
            DataPath = {data_dir}
            LogPath = {log_dir}

            [wiki]
            WikiWriteDebounceSec = 1
            EventDebounceMs = 50
            ShutdownFlushTimeoutSec = 1
        """)
    )

    proc = subprocess.Popen(
        [wiki_binary, "-c", str(conf_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Wait for wiki.url to appear (wiki writes it after binding)
    url_file = run_dir / "wiki.url"
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if url_file.exists():
            content = url_file.read_text().strip()
            if content.startswith("http"):
                break
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.skip(
                f"nimoos-wiki exited early (rc={proc.returncode}). "
                f"Output:\n{out[:800]}"
            )
        time.sleep(0.1)
    else:
        proc.terminate()
        out = proc.stdout.read() if proc.stdout else ""
        pytest.skip(
            f"nimoos-wiki did not write wiki.url within 20 s. "
            f"Output:\n{out[:800]}"
        )

    wiki_url = url_file.read_text().strip()

    # Wait for HTTP to be ready.
    # wiki has no /ping endpoint; use the internal file-events endpoint which is
    # localhost-only, JWT-exempt, and returns 200 with an empty event list.
    if not _wait_http(
        f"{wiki_url}/v1/wiki/_internal/file-events", timeout=15.0
    ):
        proc.terminate()
        out = proc.stdout.read() if proc.stdout else ""
        pytest.skip(
            f"nimoos-wiki HTTP not reachable at {wiki_url} within 15 s. "
            f"Output:\n{out[:800]}"
        )

    yield wiki_url

    # Graceful shutdown
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Actual test
# ---------------------------------------------------------------------------

# Import the modules under test; the agent/ directory must be on sys.path.
# When run via ``pytest agent/tests/`` from the agent/ directory this is the
# default; otherwise adjust PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # agent/

try:
    from wiki_client import WikiClient
    from wiki_context import WikiContextBuilder

    _IMPORTS_OK = True
except ImportError as _import_err:
    _IMPORTS_OK = False
    _IMPORT_ERR = str(_import_err)


@pytest.mark.asyncio
async def test_register_then_get_then_append_then_context(running_wiki, tmp_path):
    """Full chain: register → get node → put user notes → context builder."""

    if not _IMPORTS_OK:
        pytest.skip(f"Cannot import wiki_client / wiki_context: {_IMPORT_ERR}")

    # --- target directory -----------------------------------------------
    target = tmp_path / "project_x"
    target.mkdir()
    (target / "README.md").write_text("hello from e2e test\n")

    c = WikiClient(user_id="42", base_url=running_wiki)
    try:
        # 1. Register the root
        reg = await c.post_root(str(target), "space")
        # Response should include at least an id or the path
        assert "id" in reg or reg.get("path") == str(target), (
            f"Unexpected register response: {reg!r}"
        )

        c.reset_cache()

        # 2. Poll until wiki creates the node in its DB
        node = None
        for _ in range(60):
            c.invalidate_node(str(target))
            node = await c.get_node(str(target))
            if node is not None:
                break
            time.sleep(0.15)

        assert node is not None, (
            "wiki did not create a node for the registered root within ~9 s"
        )
        assert "etag" in node, f"node missing 'etag': {node!r}"

        # 3. Append user notes
        await c.put_user_notes(str(target), "first note\n", if_match=node["etag"])

        # 4. Verify notes are visible via a fresh get
        c.invalidate_node(str(target))
        node2 = await c.get_node(str(target))
        assert node2 is not None, "node disappeared after writing user notes"
        assert "first note" in (node2.get("user_notes") or ""), (
            f"user_notes not updated: {node2!r}"
        )

        # 5. Tree endpoint with root_id returns the registered node.
        root_id = reg["id"]
        tree = await c.list_full_tree(root_id=root_id)
        assert any(n["path"] == str(target) for n in tree), (
            f"target path {str(target)!r} not in per-root tree (root_id={root_id!r}). "
            f"Tree: {tree!r}"
        )
        assert tree[0].get("level") == "space", (
            f"registered node should have level='space', got: {tree[0]!r}"
        )

        # 6. Tree endpoint with NO root_id returns all roots (the path
        # WikiContextBuilder uses).  Drop the per-instance cache first so we
        # actually hit the network for the "" key.
        c.reset_cache()
        full_tree = await c.list_full_tree()
        assert any(n["path"] == str(target) for n in full_tree), (
            f"target path {str(target)!r} not in full tree (no root_id). "
            f"Tree: {full_tree!r}"
        )

        # 7. WikiContextBuilder.build() — the production injection path.
        # Builder calls list_full_tree() (no root_id), groups by space, and
        # renders the map block.  The registered root should appear.
        c.reset_cache()
        block = await WikiContextBuilder(c).build(user_patterns=[])
        assert "NimoOS 存储空间地图" in block
        assert "用户笔记" in block
        assert str(target) in block, (
            f"registered root path {str(target)!r} missing from builder block:\n{block}"
        )

    finally:
        await c.aclose()
