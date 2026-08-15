"""Tests for the headless lark-cli device-flow binding state machine.

The fake CLI below is a bash script that reproduces the *recorded* behaviour of
the real lark-cli v1.0.85 (raw captures in tests/fixtures/lark/), most
importantly:

  * every step writes its JSON / QR / URL output to **stderr**, not stdout;
  * "not configured" exits 3 with {"ok":false,"error":{"subtype":
    "not_configured"}};
  * `config init --new` has no --json and only prints a bare URL line, then
    blocks until the user finishes in the browser.

It keeps state in $HOME/.lark-cli so the DELETE path (which wipes that dir) is
genuinely exercised rather than mocked away.

The state-machine tests drive `binding` directly on a real event loop rather
than through TestClient: TestClient without a `with` block tears its anyio
portal (and therefore the loop) down after every request, so a background
`asyncio.create_task` would never run to completion. The HTTP layer is covered
separately with stubbed binding calls.
"""

import asyncio
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import main
from lark import binding

H = {"X-User-Id": "u1"}
UID = "u1"

VERIFY_URL_INIT = "https://open.feishu.cn/page/cli?user_code=ZA28-RX4U&lpv=1.0.85&from=cli"
VERIFY_URL_LOGIN = "https://open.feishu.cn/suite/passport/oauth/device?user_code=WXYZ-1234"
DEVICE_CODE = "dev-code-abc123"

NOT_CONFIGURED = json.dumps(
    {
        "ok": False,
        "error": {
            "type": "config",
            "subtype": "not_configured",
            "message": "not configured",
            "hint": "run `lark-cli config init --new` in the background.",
        },
    }
)

# NOTE: the fake's own control paths are baked into the script text, not passed
# through the environment — binding._env() strips everything except HOME/PATH/
# LANG (that is the point of the env-hygiene test), so an env-var-driven fake
# would silently misbehave.
FAKE_CLI = r"""#!/bin/bash
# Fake lark-cli. Mirrors the recorded v1.0.85 behaviour: output on stderr,
# exit 3 when not configured. State lives in $HOME/.lark-cli.
env > "__ENV_DUMP__"
D="$HOME/.lark-cli"
not_configured() {
  cat >&2 <<'EOF'
__NOT_CONFIGURED__
EOF
  exit 3
}

case "$1 $2" in
  "config show")
    [ -f "$D/config.json" ] || not_configured
    echo '{"ok":true,"app_id":"cli_fake"}' >&2
    ;;
  "config init")
    if [ -f "__INIT_FAIL__" ]; then
      echo '{"ok":false,"error":{"message":"init boom"}}' >&2
      exit 4
    fi
    echo "  QR-CODE-BLOCK  " >&2
    echo "" >&2
    echo "打开以下链接配置应用:" >&2
    echo "" >&2
    echo "  __URL_INIT__" >&2
    echo "" >&2
    echo "等待配置应用..." >&2
    # Block until the "user" finishes in the browser.
    for _ in $(seq 1 400); do
      [ -f "__INIT_DONE__" ] && break
      sleep 0.05
    done
    mkdir -p "$D" && echo '{}' > "$D/config.json"
    ;;
  "auth login")
    [ -f "$D/config.json" ] || not_configured
    if [ "$3" = "--device-code" ]; then
      [ "$4" = "__DEVICE_CODE__" ] || { echo '{"ok":false,"error":{"message":"bad code"}}' >&2; exit 5; }
      mkdir -p "$D" && echo '{}' > "$D/token.json"
      echo '{"ok":true,"data":{"user_name":"Probe User"}}' >&2
    else
      echo '{"ok":true,"data":{"device_code":"__DEVICE_CODE__","verification_url":"__URL_LOGIN__","expires_in":600}}' >&2
    fi
    ;;
  "auth logout")
    echo "Logged out." >&2
    ;;
  "whoami"*)
    [ -f "$D/config.json" ] || not_configured
    [ -f "$D/token.json" ] || { echo '{"ok":false,"error":{"type":"auth","subtype":"not_logged_in"}}' >&2; exit 3; }
    echo '{"ok":true,"identity":"user","user":{"name":"Probe User","open_id":"ou_fake"}}'
    ;;
  *)
    echo '{"ok":false,"error":{"type":"validation"}}' >&2
    exit 2
    ;;
esac
exit 0
"""


@pytest.fixture
def lark_env(tmp_path, monkeypatch):
    """Install the fake CLI + a throwaway HOMES_ROOT, and reset module state."""
    from skills import shell

    homes = tmp_path / "homes"
    homes.mkdir()
    monkeypatch.setattr(shell, "HOMES_ROOT", homes)

    env_dump = tmp_path / "env.dump"
    init_done = tmp_path / "init.done"
    init_fail = tmp_path / "init.fail"

    script = (
        FAKE_CLI.replace("__NOT_CONFIGURED__", NOT_CONFIGURED)
        .replace("__URL_INIT__", VERIFY_URL_INIT)
        .replace("__URL_LOGIN__", VERIFY_URL_LOGIN)
        .replace("__DEVICE_CODE__", DEVICE_CODE)
        .replace("__ENV_DUMP__", str(env_dump))
        .replace("__INIT_DONE__", str(init_done))
        .replace("__INIT_FAIL__", str(init_fail))
    )
    fake = tmp_path / "lark-cli"
    fake.write_text(script)
    fake.chmod(0o755)

    monkeypatch.setenv("NIMOOS_LARK_CLI", str(fake))
    binding.reset_all()
    try:
        yield {
            "homes": homes,
            "env_dump": env_dump,
            "init_done": init_done,
            "init_fail": init_fail,
            "bin": fake,
        }
    finally:
        binding.reset_all()


def run_async(coro):
    return asyncio.run(coro)


async def _await_phase(want, timeout=20.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await binding.status(UID)
        if last["phase"] == want:
            return last
        if last["phase"] == "failed" and want != "failed":
            break
        await asyncio.sleep(0.05)
    raise AssertionError(f"phase never reached {want!r}; last={last!r}")


# --------------------------------------------------------------------------
# module-level contract
# --------------------------------------------------------------------------


def test_lark_bin_default_and_env_override(monkeypatch):
    monkeypatch.delenv("NIMOOS_LARK_CLI", raising=False)
    assert binding.LARK_BIN == "/opt/toolbox/bin/lark-cli"
    assert binding.lark_bin() == "/opt/toolbox/bin/lark-cli"
    monkeypatch.setenv("NIMOOS_LARK_CLI", "/tmp/fake-lark")
    assert binding.lark_bin() == "/tmp/fake-lark"


def test_user_home_reuses_shell_homes_root(tmp_path, monkeypatch):
    from skills import shell

    monkeypatch.setattr(shell, "HOMES_ROOT", tmp_path)
    home = binding.user_home(UID)
    assert home == tmp_path / UID
    assert home.is_dir()  # lazily created


def test_log_is_truncated_to_8kb():
    assert binding.MAX_LOG_BYTES == 8 * 1024
    assert len(binding._clip("x" * 40000).encode()) <= binding.MAX_LOG_BYTES


# --------------------------------------------------------------------------
# state machine (real subprocesses against the fake CLI)
# --------------------------------------------------------------------------


def test_unknown_user_is_unbound(lark_env):
    async def scenario():
        return await binding.status("nobody")

    st = run_async(scenario())
    assert st["phase"] == "unbound"
    assert st["verify_url"] == ""
    assert st["identity"] is None


def test_full_device_flow_to_bound_then_unbind(lark_env):
    async def scenario():
        snap = await binding.start(UID)
        assert snap["phase"] in ("starting", "await_verify")

        # Step 2: config init --new blocks and prints the URL on stderr.
        st = await _await_phase("await_verify")
        assert st["verify_url"] == VERIFY_URL_INIT
        assert st["log"]  # recent step log surfaced for UI troubleshooting

        # The "user" completes app setup in the browser -> init exits.
        lark_env["init_done"].write_text("ok")

        # Steps 3/4: auth login --no-wait --json -> device code -> bound.
        st = await _await_phase("bound")
        assert st["identity"] is not None
        assert st["identity"]["user"]["name"] == "Probe User"
        assert st["error"] == ""

        # Idempotent: starting again once bound just reports the current state.
        assert (await binding.start(UID))["phase"] == "bound"

        # DELETE wipes the CLI state dir; status then re-probes -> unbound.
        home = lark_env["homes"] / UID
        assert (home / ".lark-cli").is_dir()
        await binding.unbind(UID)
        assert not (home / ".lark-cli").exists()

        st = await binding.status(UID)
        assert st["phase"] == "unbound"
        assert st["identity"] is None

    run_async(scenario())


def test_verify_url_is_scraped_from_stderr(lark_env):
    """Regression pin for the recorded behaviour: lark-cli writes the QR block
    and the verification URL to stderr and leaves stdout empty, and it does so
    *while still running*. Reading only stdout, or only reading after exit,
    would leave the flow stuck in `starting` forever."""

    async def scenario():
        await binding.start(UID)
        st = await _await_phase("await_verify")
        assert st["verify_url"] == VERIFY_URL_INIT
        assert " " not in st["verify_url"]
        # query params must survive verbatim (opaque-string rule)
        assert "&from=cli" in st["verify_url"]
        await binding.unbind(UID)

    run_async(scenario())


def test_start_is_idempotent_while_in_flight(lark_env):
    async def scenario():
        await binding.start(UID)
        await _await_phase("await_verify")
        task = binding._STATES[UID].task
        snap = await binding.start(UID)
        assert snap["phase"] == "await_verify"
        assert snap["verify_url"] == VERIFY_URL_INIT
        # no second task spawned
        assert binding._STATES[UID].task is task
        await binding.unbind(UID)

    run_async(scenario())


def test_failed_step_sets_failed_phase_with_stderr_summary(lark_env):
    lark_env["init_fail"].write_text("1")

    async def scenario():
        await binding.start(UID)
        st = await _await_phase("failed")
        assert "init boom" in st["error"]
        assert st["error"].startswith("config init")
        assert st["identity"] is None

    run_async(scenario())


def test_bad_device_code_fails_with_summary(lark_env, monkeypatch):
    """Step 4 non-zero exit -> failed + stderr summary (not a silent hang)."""
    lark_env["init_done"].write_text("ok")

    async def scenario():
        real_find = binding._find_key

        def wrong_code(doc, keys):
            if "device_code" in tuple(keys):
                return "not-the-right-code"
            return real_find(doc, keys)

        monkeypatch.setattr(binding, "_find_key", wrong_code)
        await binding.start(UID)
        st = await _await_phase("failed")
        assert "bad code" in st["error"]

    run_async(scenario())


def test_subprocess_env_is_minimal_and_carries_no_provider_key(lark_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("NIMOOS_AGENT_PROVIDER_KEY", "sk-also-must-not-leak")

    async def scenario():
        await binding.start(UID)
        await _await_phase("await_verify")
        await binding.unbind(UID)

    run_async(scenario())

    dumped = dict(
        line.split("=", 1)
        for line in lark_env["env_dump"].read_text().splitlines()
        if "=" in line
    )
    assert dumped["HOME"] == str(lark_env["homes"] / UID)
    assert "LANG" in dumped
    toolbox_bin = os.path.dirname(str(lark_env["bin"]))
    assert dumped["PATH"].startswith(toolbox_bin + ":")
    leaked = [k for k in dumped if "KEY" in k.upper() or k.startswith("NIMOOS_")]
    assert not leaked, f"leaked env: {leaked}"
    assert set(dumped) <= {"HOME", "PATH", "LANG", "PWD", "SHLVL", "_"}


def test_unbind_when_never_bound_is_a_noop(lark_env):
    async def scenario():
        await binding.unbind(UID)
        return await binding.status(UID)

    assert run_async(scenario())["phase"] == "unbound"


def test_unbind_cancels_in_flight_task(lark_env):
    async def scenario():
        await binding.start(UID)
        await _await_phase("await_verify")
        task = binding._STATES[UID].task
        await binding.unbind(UID)
        assert task.cancelled() or task.done()
        st = await binding.status(UID)
        assert st["phase"] == "unbound"
        assert st["verify_url"] == ""

    run_async(scenario())


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


def _client():
    # No `with` block: entering TestClient as a context manager runs FastAPI
    # lifespan, which re-runs the MCP session manager singleton. Same reason as
    # tests/test_toolbox_endpoints.py.
    return TestClient(main.app)


@pytest.fixture
def stub_binding(monkeypatch):
    calls = []
    state = {
        "phase": "await_verify",
        "verify_url": VERIFY_URL_INIT,
        "identity": None,
        "error": "",
        "log": "tail",
    }

    async def fake_start(uid):
        calls.append(("start", uid))
        return dict(state, phase="starting")

    async def fake_status(uid):
        calls.append(("status", uid))
        return dict(state)

    async def fake_unbind(uid):
        calls.append(("unbind", uid))

    monkeypatch.setattr(binding, "start", fake_start)
    monkeypatch.setattr(binding, "status", fake_status)
    monkeypatch.setattr(binding, "unbind", fake_unbind)
    return calls


def test_post_returns_202_with_phase(stub_binding):
    r = _client().post("/agent/lark/binding", headers=H)
    assert r.status_code == 202
    assert r.json()["phase"] == "starting"
    assert stub_binding == [("start", UID)]


def test_get_returns_full_snapshot(stub_binding):
    r = _client().get("/agent/lark/binding", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"phase", "verify_url", "identity", "error", "log"}
    assert body["verify_url"] == VERIFY_URL_INIT
    assert stub_binding == [("status", UID)]


def test_delete_returns_204(stub_binding):
    r = _client().delete("/agent/lark/binding", headers=H)
    assert r.status_code == 204
    assert r.content == b""
    assert stub_binding == [("unbind", UID)]


def test_endpoints_require_user_header(stub_binding):
    # main.py rejects a missing X-User-Id at the auth layer (401) before
    # FastAPI's own required-header validation (422) gets a look in.
    c = _client()
    assert c.get("/agent/lark/binding").status_code in (401, 422)
    assert c.post("/agent/lark/binding").status_code in (401, 422)
    assert c.delete("/agent/lark/binding").status_code in (401, 422)


def test_get_never_404s_for_unknown_user(stub_binding):
    r = _client().get("/agent/lark/binding", headers={"X-User-Id": "nobody"})
    assert r.status_code == 200


# --------------------------------------------------------------------------
# JSON parsing helpers (shapes recorded from the real CLI)
# --------------------------------------------------------------------------


def test_parse_json_ignores_non_json_preamble():
    raw = "[lark-cli] [WARN] proxy detected: HTTP_PROXY=x\n" + json.dumps({"ok": True, "a": 1})
    assert binding._parse_json(raw) == {"ok": True, "a": 1}


def test_parse_json_returns_none_on_garbage():
    assert binding._parse_json("no json here at all") is None
    assert binding._parse_json("") is None


def test_find_key_searches_recursively():
    doc = {"ok": True, "data": {"nested": {"verification_url": "https://x", "device_code": "c1"}}}
    assert binding._find_key(doc, ("verification_url",)) == "https://x"
    assert binding._find_key(doc, ("device_code",)) == "c1"
    assert binding._find_key(doc, ("nope",)) is None


def test_find_key_honours_alias_order():
    doc = {"verification_uri": "https://b", "data": {"verification_url": "https://a"}}
    assert binding._find_key(doc, ("verification_url", "verification_uri")) == "https://a"
    assert (
        binding._find_key({"verification_uri": "https://b"},
                          ("verification_url", "verification_uri")) == "https://b"
    )


def _fixture(name):
    return (pathlib.Path(__file__).parent / "fixtures" / "lark" / name).read_text()


def test_not_configured_fixture_is_detected():
    """Recorded from the real CLI: the error envelope arrives on stderr."""
    for name in (
        "config_show_not_configured.stderr.json",
        "auth_status_not_configured.stderr.json",
        "whoami_not_configured.stderr.json",
        "auth_login_nowait_not_configured.stderr.json",
    ):
        doc = binding._parse_json(_fixture(name))
        assert doc is not None, name
        assert doc["ok"] is False, name
        assert doc["error"]["subtype"] == "not_configured", name


def test_url_regex_matches_recorded_config_init_output():
    url = binding._scrape_url(_fixture("config_init_new.stderr.txt"))
    assert url is not None
    assert url.startswith("https://open.feishu.cn/page/cli?user_code=")
    # opaque string: query params must survive intact
    assert "&from=cli" in url
    # the ASCII QR block that precedes the URL must not confuse the scrape
    assert "█" not in url
