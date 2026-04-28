# 模型思考强度选择 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 AI Agent 页面给用户加上"思考开关 + 强度选择"的控件，前后端打通；首期接 DeepSeek（深度可调），同步把 Anthropic 通道现有的"max_tokens 自动 thinking"硬编码改成由 ThinkingConfig 控制；OpenAI 适配器代码就位但不专门测。

**Architecture:**
- 统一 4 档 `ThinkingLevel` 枚举（`low`/`medium`/`high`/`max`）+ 独立 `enabled` 开关，前端发请求时携带，后端按 provider type 翻译为各家原生参数。
- Per-session 持久化（Python `sessions` 表加列）+ 全局默认（Python 新建 `user_settings` 表）。
- 模型能力表（Go `service/model_capability.go`）把 provider type + model name → `supports_thinking`，前端用此灰显不支持的模型的思考栏。

**Tech Stack:** Go (echo + sqlite3) · Python (FastAPI + openai-agents SDK + sqlite3) · Vue 3 (Composition API + agentStore)

**Spec:** [`../specs/2026-04-28-thinking-intensity-design.md`](../specs/2026-04-28-thinking-intensity-design.md)

---

## 文件结构总览

### 新建文件

| 路径 | 责任 |
|---|---|
| `agent/provider_adapters.py` | provider type → ModelSettings/extra_body 映射 |
| `agent/tests/test_provider_adapters.py` | 上述模块的单元测试 |
| `service/model_capability.go` | 模型能力规则表（`SupportsThinking` 函数） |
| `service/model_capability_test.go` | 上述的单元测试 |
| `service/provider_classify.go` | base_url → provider_type 启发式分类 |
| `service/provider_classify_test.go` | 上述的单元测试 |
| `NimoOS-UI/src/views/AI/Agent/shell/ThinkingBar.vue` | 思考栏组件 |
| `NimoOS-UI/src/views/AI/Settings/sections/ThinkingDefaultsSection.vue` | 设置页区块 |

### 修改文件

| 路径 | 改动概要 |
|---|---|
| `agent/db.py` | sessions 加 thinking 列；新建 user_settings 表 |
| `agent/main.py` | RunRequest 加 thinking 字段；新增 PATCH session thinking + GET/PUT user_settings 端点 |
| `agent/agent.py` | `AgentRunner.run` 接收 thinking + provider_type，构造 ModelSettings 注入 |
| `service/db.go` | providers 表加 `provider_type` 列 + 启动时自动分类回填 |
| `service/provider.go` | `/v1/ai/providers` 响应加 `provider_type` + `supports_thinking` |
| `service/cloud_anthropic.go` | 删除 max_tokens 自动 thinking 逻辑；改成读 ThinkingConfig |
| `service/cloud_anthropic_test.go` | 测试 ThinkingConfig 各值的转换正确性 |
| `route/v2/chat.go` | 读取入参里的 thinking 字段，传给 cloud_anthropic |
| `route/v2/agent.go` | 把 thinking header 透传给 Python（如需） |
| `NimoOS-UI/src/views/AI/Agent/shell/AgentTopbar.vue` | 在 ModelPicker 下方挂载 ThinkingBar |
| `NimoOS-UI/src/views/AI/Agent/store/agentStore.js` | thinking state + 持久化 + 发请求时携带 |
| `NimoOS-UI/src/api/ai.js`（或对应 API 包装） | listProviders 解析 supports_thinking + new endpoints |

---

## Task 1: Python — sessions 表加 thinking 列 + user_settings 表

**Files:**
- Modify: `agent/db.py:8-101` (schema string)
- Test: `agent/tests/test_db.py` (新建或扩展)

- [ ] **Step 1: 写失败测试**

新建 `agent/tests/test_db_thinking.py`：

```python
import sqlite3
from pathlib import Path

import db as db_module


def test_sessions_has_thinking_columns(tmp_path):
    db_path = str(tmp_path / "agent.db")
    snaps = str(tmp_path / "snaps")
    conn = db_module.init_db(path=db_path, snapshots_root=snaps)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "thinking_enabled" in cols
    assert "thinking_level" in cols


def test_user_settings_table_exists(tmp_path):
    db_path = str(tmp_path / "agent.db")
    snaps = str(tmp_path / "snaps")
    conn = db_module.init_db(path=db_path, snapshots_root=snaps)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_settings)")}
    assert {"user_id", "key", "value"} <= cols


def test_old_db_migrates_idempotently(tmp_path):
    """An existing DB without thinking columns gets them added on init."""
    db_path = str(tmp_path / "agent.db")
    snaps = str(tmp_path / "snaps")
    pre = sqlite3.connect(db_path)
    pre.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
    """)
    pre.commit()
    pre.close()
    conn = db_module.init_db(path=db_path, snapshots_root=snaps)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "thinking_enabled" in cols
    assert "thinking_level" in cols
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /home/nimo/nimo_os/NimoOS-AI/agent
pytest tests/test_db_thinking.py -v
```
预期：3 个用例都 FAIL（列不存在 / 表不存在）。

- [ ] **Step 3: 修改 `agent/db.py` 的 schema**

在 `_SCHEMA` 中加入 user_settings 表（接在 sessions 表之后）：

```python
CREATE TABLE IF NOT EXISTS user_settings (
    user_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, key)
);
```

把 sessions 的 CREATE 改成包含 thinking 列：

```python
CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    title             TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    thinking_enabled  INTEGER,
    thinking_level    TEXT
);
```

- [ ] **Step 4: 在 `init_db()` 末尾加幂等列迁移**

在 `conn.executescript(_SCHEMA)` 之后、`conn.execute("PRAGMA foreign_keys=ON")` 之前加：

```python
    # Idempotent ALTER for existing databases without thinking columns.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "thinking_enabled" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN thinking_enabled INTEGER")
    if "thinking_level" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN thinking_level TEXT")
```

- [ ] **Step 5: 运行测试，确认通过**

```bash
pytest tests/test_db_thinking.py -v
```
预期：3 个用例全 PASS。

- [ ] **Step 6: Commit**

```bash
git add agent/db.py agent/tests/test_db_thinking.py
git commit -m "feat(ai/db): sessions thinking columns + user_settings table"
```

---

## Task 2: Python — provider_adapters 模块（核心翻译层）

**Files:**
- Create: `agent/provider_adapters.py`
- Test: `agent/tests/test_provider_adapters.py`

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_provider_adapters.py
import pytest
from provider_adapters import (
    ThinkingLevel, ThinkingConfig, build_model_settings, ProviderType,
)


def test_deepseek_disabled():
    s = build_model_settings(
        ProviderType.DEEPSEEK,
        ThinkingConfig(enabled=False, level=ThinkingLevel.MEDIUM),
    )
    # DeepSeek expects extra_body.thinking.type == "disabled"
    assert s.extra_body == {"thinking": {"type": "disabled"}}
    # No reasoning_effort when disabled
    assert "reasoning_effort" not in (s.extra_args or {})


@pytest.mark.parametrize("level,expected_effort", [
    (ThinkingLevel.LOW,    "high"),
    (ThinkingLevel.MEDIUM, "high"),
    (ThinkingLevel.HIGH,   "max"),
    (ThinkingLevel.MAX,    "max"),
])
def test_deepseek_levels(level, expected_effort):
    s = build_model_settings(
        ProviderType.DEEPSEEK,
        ThinkingConfig(enabled=True, level=level),
    )
    assert s.extra_body == {"thinking": {"type": "enabled"}}
    assert s.extra_args["reasoning_effort"] == expected_effort


@pytest.mark.parametrize("level,expected_effort", [
    (ThinkingLevel.LOW,    "low"),
    (ThinkingLevel.MEDIUM, "medium"),
    (ThinkingLevel.HIGH,   "high"),
    (ThinkingLevel.MAX,    "high"),  # OpenAI has no max → reuse high
])
def test_openai_levels(level, expected_effort):
    s = build_model_settings(
        ProviderType.OPENAI,
        ThinkingConfig(enabled=True, level=level),
    )
    assert s.extra_args["reasoning_effort"] == expected_effort


def test_openai_disabled_uses_minimal():
    s = build_model_settings(
        ProviderType.OPENAI,
        ThinkingConfig(enabled=False, level=ThinkingLevel.MEDIUM),
    )
    assert s.extra_args["reasoning_effort"] == "minimal"


def test_other_provider_returns_empty_settings():
    """Unknown provider types skip thinking params entirely."""
    s = build_model_settings(
        ProviderType.OTHER,
        ThinkingConfig(enabled=True, level=ThinkingLevel.HIGH),
    )
    assert (s.extra_args or {}) == {}
    assert (s.extra_body or {}) == {}


def test_none_thinking_returns_empty_settings():
    s = build_model_settings(ProviderType.DEEPSEEK, None)
    assert (s.extra_args or {}) == {}
    assert (s.extra_body or {}) == {}
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_provider_adapters.py -v
```
预期：全部 FAIL（模块不存在）。

- [ ] **Step 3: 实现 `agent/provider_adapters.py`**

```python
"""Provider-specific translation of unified ThinkingConfig into ModelSettings.

The frontend speaks one 4-level scale (low/medium/high/max) plus an enabled
toggle. Each provider's API exposes thinking control differently — DeepSeek
uses extra_body + reasoning_effort, OpenAI uses reasoning_effort, Anthropic
uses thinking.budget_tokens (handled in the Go service for the Anthropic
path; this module only covers OpenAI-compatible Chat Completions).
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from agents.model_settings import ModelSettings


class ThinkingLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


@dataclass(frozen=True)
class ThinkingConfig:
    enabled: bool
    level: ThinkingLevel


class ProviderType(str, Enum):
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    QWEN = "qwen"
    OLLAMA = "ollama"
    OTHER = "other"


# DeepSeek doc: low/medium silently map to "high"; xhigh maps to "max".
_DEEPSEEK_EFFORT = {
    ThinkingLevel.LOW: "high",
    ThinkingLevel.MEDIUM: "high",
    ThinkingLevel.HIGH: "max",
    ThinkingLevel.MAX: "max",
}

# OpenAI o-series / gpt-5: minimal/low/medium/high. No "max" — reuse high.
_OPENAI_EFFORT = {
    ThinkingLevel.LOW: "low",
    ThinkingLevel.MEDIUM: "medium",
    ThinkingLevel.HIGH: "high",
    ThinkingLevel.MAX: "high",
}


def build_model_settings(
    provider_type: ProviderType,
    thinking: Optional[ThinkingConfig],
) -> ModelSettings:
    """Map (provider_type, thinking) to ModelSettings for the Agents SDK.

    Anthropic is intentionally NOT handled here — Anthropic requests are
    converted in service/cloud_anthropic.go where the budget_tokens is set.
    Returning empty settings for ANTHROPIC just means we won't double-set.
    """
    if thinking is None:
        return ModelSettings()

    if provider_type == ProviderType.DEEPSEEK:
        if not thinking.enabled:
            return ModelSettings(
                extra_body={"thinking": {"type": "disabled"}},
            )
        return ModelSettings(
            extra_body={"thinking": {"type": "enabled"}},
            extra_args={"reasoning_effort": _DEEPSEEK_EFFORT[thinking.level]},
        )

    if provider_type == ProviderType.OPENAI:
        if not thinking.enabled:
            return ModelSettings(extra_args={"reasoning_effort": "minimal"})
        return ModelSettings(
            extra_args={"reasoning_effort": _OPENAI_EFFORT[thinking.level]},
        )

    # ANTHROPIC handled in Go service. QWEN/OLLAMA/OTHER: pass through.
    return ModelSettings()
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
pytest tests/test_provider_adapters.py -v
```
预期：所有用例 PASS。

- [ ] **Step 5: Commit**

```bash
git add agent/provider_adapters.py agent/tests/test_provider_adapters.py
git commit -m "feat(ai/agent): provider_adapters module for thinking config translation"
```

---

## Task 3: Python — RunRequest 加 thinking 字段 + AgentRunner 接收并构造 ModelSettings

**Files:**
- Modify: `agent/main.py:75-79` (RunRequest model) + /run handler
- Modify: `agent/agent.py:154-217` (AgentRunner.run signature + 模型构造)
- Test: `agent/tests/test_agent_thinking.py` (新建)

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_agent_thinking.py
"""Verify RunRequest accepts thinking + AgentRunner builds correct ModelSettings."""
from unittest.mock import patch

import pytest

from main import RunRequest
from provider_adapters import ThinkingLevel


def test_run_request_accepts_thinking():
    req = RunRequest(
        message="hi",
        model="deepseek-v4-pro",
        thinking={"enabled": True, "level": "high"},
    )
    assert req.thinking is not None
    assert req.thinking.enabled is True
    assert req.thinking.level == ThinkingLevel.HIGH


def test_run_request_thinking_optional():
    req = RunRequest(message="hi", model="x")
    assert req.thinking is None


@pytest.mark.asyncio
async def test_agent_runner_passes_thinking_to_model_settings(monkeypatch):
    """When thinking config is provided, AgentRunner attaches a ModelSettings
    with the appropriate extra_body/extra_args to the model."""
    from agent import AgentRunner
    from provider_adapters import ThinkingConfig

    captured = {}

    def fake_model_init(self, *, model, openai_client, model_settings=None,
                       should_replay_reasoning_content=None, **kwargs):
        captured["model_settings"] = model_settings
        captured["model"] = model
        # Stub out the rest to avoid real network calls
        self.model = model

    monkeypatch.setattr(
        "agents.models.openai_chatcompletions.OpenAIChatCompletionsModel.__init__",
        fake_model_init,
    )

    # Stub Runner.run_streamed to immediately end without making API calls
    class _FakeStream:
        def stream_events(self):
            async def gen():
                if False:
                    yield None
            return gen()
        def to_input_list(self): return []
        final_output = ""

    monkeypatch.setattr("agent.Runner.run_streamed", lambda *a, **k: _FakeStream())

    # Build minimal sink
    class _Sink:
        async def put(self, ev): pass

    import sqlite3, db as db_module
    conn = db_module.init_db(path=":memory:", snapshots_root="/tmp/snaps_test")
    conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) "
                 "VALUES('s1','u1','t',1,1)")
    conn.commit()

    runner = AgentRunner(conn)
    await runner.run(
        session_id="s1", user_id="u1", message="hi",
        sink=_Sink(),
        provider_key="k", provider_url="http://x", model_name="deepseek-v4-pro",
        provider_type="deepseek",
        thinking=ThinkingConfig(enabled=True, level=ThinkingLevel.HIGH),
    )

    ms = captured["model_settings"]
    assert ms is not None
    assert ms.extra_body == {"thinking": {"type": "enabled"}}
    assert ms.extra_args["reasoning_effort"] == "max"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_agent_thinking.py -v
```
预期：FAIL（RunRequest 无 thinking 字段；AgentRunner.run 不接受 thinking/provider_type）。

- [ ] **Step 3: 改 `agent/main.py` 的 RunRequest**

把现有 RunRequest（约 75-79 行）改为：

```python
from typing import Optional
from pydantic import BaseModel

from provider_adapters import ThinkingLevel


class ThinkingConfigPayload(BaseModel):
    enabled: bool
    level: ThinkingLevel


class RunRequest(BaseModel):
    message: str
    model: str = "gpt-4o-mini"
    kind: str = "chat"          # 'chat' | 'init'
    init_target: Optional[str] = None
    thinking: Optional[ThinkingConfigPayload] = None
```

- [ ] **Step 4: 修改 /run handler 合并 thinking 三层来源**

定位现有 `/agent/sessions/{session_id}/run` handler（main.py 约 717-761 行），在调用 `_start_run()` 前插入：

```python
    # Resolve thinking config: request body → session row → user_settings default.
    thinking_cfg = None
    if req.thinking is not None:
        thinking_cfg = ThinkingConfig(
            enabled=req.thinking.enabled,
            level=req.thinking.level,
        )
    else:
        row = db_conn.execute(
            "SELECT thinking_enabled, thinking_level FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row and row["thinking_enabled"] is not None and row["thinking_level"]:
            thinking_cfg = ThinkingConfig(
                enabled=bool(row["thinking_enabled"]),
                level=ThinkingLevel(row["thinking_level"]),
            )
        else:
            # Fall back to user-level defaults; if missing, hard-code.
            defaults = _read_user_thinking_defaults(db_conn, user_id)
            thinking_cfg = defaults  # may be a ThinkingConfig

    provider_type = request.headers.get("X-Agent-Provider-Type", "other")
```

(Add helper `_read_user_thinking_defaults` in main.py — Task 4 covers it; for now stub it to return `ThinkingConfig(True, ThinkingLevel.MEDIUM)`.)

Pass through to `_start_run`:

```python
    await _start_run(
        ...,  # existing args
        thinking=thinking_cfg,
        provider_type=provider_type,
    )
```

Update `_start_run` signature to accept and forward both into `AgentRunner.run()`.

- [ ] **Step 5: 改 `agent/agent.py` 的 AgentRunner.run**

更新 `run()` 签名（agent.py 约 154-168 行），新增两个 kwargs：

```python
    async def run(
        self,
        session_id: str,
        user_id: str,
        message: str,
        sink,
        provider_key: str,
        provider_url: str,
        model_name: str,
        *,
        provider_type: str = "other",
        thinking: "ThinkingConfig | None" = None,
        kind: str = "chat",
        chat_username: str = "",
        user_patterns: list | None = None,
        run_id: str = "",
    ) -> None:
```

在 agent.py 顶部 import：

```python
from provider_adapters import (
    ProviderType, ThinkingConfig, build_model_settings,
)
```

修改 `OpenAIChatCompletionsModel` 构造（约 203-207 行）：

```python
            try:
                pt = ProviderType(provider_type)
            except ValueError:
                pt = ProviderType.OTHER
            model_settings = build_model_settings(pt, thinking)

            model = OpenAIChatCompletionsModel(
                model=model_name,
                openai_client=client,
                model_settings=model_settings,
                should_replay_reasoning_content=default_should_replay_reasoning_content,
            )
```

> Verify the SDK accepts `model_settings` on `OpenAIChatCompletionsModel.__init__`. If not (older SDK), pass via `Agent(..., model_settings=...)` instead.

- [ ] **Step 6: 运行测试，确认通过**

```bash
pytest tests/test_agent_thinking.py -v
```
预期：3 个用例全 PASS。

- [ ] **Step 7: 跑现有的全套测试不破坏回归**

```bash
pytest tests/ -v
```
预期：无新失败（现有 test_agent.py / test_main.py 仍然全 PASS）。

- [ ] **Step 8: Commit**

```bash
git add agent/main.py agent/agent.py agent/tests/test_agent_thinking.py
git commit -m "feat(ai/agent): RunRequest.thinking + ModelSettings injection"
```

---

## Task 4: Python — settings/session 端点（GET/PUT 全局默认 + PATCH session 思考）

**Files:**
- Modify: `agent/main.py` (新增 4 个端点)
- Test: `agent/tests/test_settings_endpoints.py` (新建)

- [ ] **Step 1: 写失败测试**

```python
# agent/tests/test_settings_endpoints.py
import pytest
from httpx import AsyncClient, ASGITransport

import main as main_module


@pytest.mark.asyncio
async def test_get_thinking_defaults_returns_initial(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/agent/user-settings/thinking",
                         headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"enabled": True, "level": "medium"}


@pytest.mark.asyncio
async def test_put_then_get_thinking_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.put(
            "/agent/user-settings/thinking",
            headers={"X-User-Id": "u1"},
            json={"enabled": False, "level": "high"},
        )
        r = await ac.get("/agent/user-settings/thinking",
                         headers={"X-User-Id": "u1"})
    assert r.json() == {"enabled": False, "level": "high"}


@pytest.mark.asyncio
async def test_patch_session_thinking(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "_DB_PATH", str(tmp_path / "agent.db"))
    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/agent/sessions",
                      headers={"X-User-Id": "u1"},
                      json={"title": "t"})
        # find the session id via list endpoint (existing)
        r = await ac.get("/agent/sessions", headers={"X-User-Id": "u1"})
        sid = r.json()[0]["id"]

        r = await ac.patch(
            f"/agent/sessions/{sid}/thinking",
            headers={"X-User-Id": "u1"},
            json={"enabled": True, "level": "max"},
        )
        assert r.status_code == 200
        # Verify by reading the session row directly
        conn = main_module._db()
        row = conn.execute(
            "SELECT thinking_enabled, thinking_level FROM sessions WHERE id=?",
            (sid,),
        ).fetchone()
        assert row["thinking_enabled"] == 1
        assert row["thinking_level"] == "max"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_settings_endpoints.py -v
```
预期：404（端点不存在）。

- [ ] **Step 3: 在 `agent/main.py` 加端点**

```python
import json
import time

from provider_adapters import ThinkingLevel
# (ThinkingConfigPayload already imported from Task 3)


def _read_user_thinking_defaults(conn, user_id: str):
    """Return ThinkingConfig from user_settings, or hard-coded default."""
    from provider_adapters import ThinkingConfig
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key='thinking_default'",
        (user_id,),
    ).fetchone()
    if not row:
        return ThinkingConfig(enabled=True, level=ThinkingLevel.MEDIUM)
    try:
        v = json.loads(row["value"])
        return ThinkingConfig(
            enabled=bool(v.get("enabled", True)),
            level=ThinkingLevel(v.get("level", "medium")),
        )
    except (json.JSONDecodeError, ValueError):
        return ThinkingConfig(enabled=True, level=ThinkingLevel.MEDIUM)


@app.get("/agent/user-settings/thinking")
async def get_thinking_defaults(request: Request):
    user_id = request.headers.get("X-User-Id", "")
    cfg = _read_user_thinking_defaults(_db(), user_id)
    return {"enabled": cfg.enabled, "level": cfg.level.value}


@app.put("/agent/user-settings/thinking")
async def put_thinking_defaults(request: Request, body: ThinkingConfigPayload):
    user_id = request.headers.get("X-User-Id", "")
    conn = _db()
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) "
        "VALUES(?, 'thinking_default', ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (user_id, json.dumps({"enabled": body.enabled,
                              "level": body.level.value}),
         int(time.time())),
    )
    conn.commit()
    return {"ok": True}


@app.patch("/agent/sessions/{session_id}/thinking")
async def patch_session_thinking(session_id: str, request: Request,
                                  body: ThinkingConfigPayload):
    user_id = request.headers.get("X-User-Id", "")
    conn = _db()
    cur = conn.execute(
        "UPDATE sessions SET thinking_enabled=?, thinking_level=?, updated_at=? "
        "WHERE id=? AND user_id=?",
        (1 if body.enabled else 0, body.level.value,
         int(time.time()), session_id, user_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "session not found")
    return {"ok": True}
```

> If `_db()` helper doesn't exist, use whatever the rest of main.py uses to get the connection (likely `db_module.get_connection()`).

- [ ] **Step 4: 运行测试，确认通过**

```bash
pytest tests/test_settings_endpoints.py -v
```
预期：3 个用例全 PASS。

- [ ] **Step 5: Commit**

```bash
git add agent/main.py agent/tests/test_settings_endpoints.py
git commit -m "feat(ai/agent): user-settings + per-session thinking endpoints"
```

---

## Task 5: Go — providers 表 provider_type 列 + 自动分类

**Files:**
- Create: `service/provider_classify.go`, `service/provider_classify_test.go`
- Modify: `service/db.go:86-153` (migrate)
- Modify: `service/provider.go` (struct + UPSERT/SELECT)

- [ ] **Step 1: 写失败测试**

```go
// service/provider_classify_test.go
package service

import "testing"

func TestClassifyByBaseURL(t *testing.T) {
	cases := []struct {
		baseURL  string
		protocol Protocol
		want     string
	}{
		{"https://api.deepseek.com", ProtocolOpenAI, "deepseek"},
		{"https://api.deepseek.com/v1", ProtocolOpenAI, "deepseek"},
		{"https://api.openai.com/v1", ProtocolOpenAI, "openai"},
		{"https://dashscope.aliyuncs.com/compatible-mode/v1", ProtocolOpenAI, "qwen"},
		{"https://api.anthropic.com/v1", ProtocolAnthropic, "anthropic"},
		{"http://127.0.0.1:11434/v1", ProtocolOpenAI, "ollama"},
		{"https://my-llm.example.com", ProtocolOpenAI, "other"},
	}
	for _, c := range cases {
		got := ClassifyProvider(c.baseURL, c.protocol)
		if got != c.want {
			t.Errorf("ClassifyProvider(%q,%q) = %q, want %q",
				c.baseURL, c.protocol, got, c.want)
		}
	}
}
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /home/nimo/nimo_os/NimoOS-AI && go test ./service/ -run TestClassifyByBaseURL -v
```
预期：FAIL（ClassifyProvider 未定义）。

- [ ] **Step 3: 实现 `service/provider_classify.go`**

```go
package service

import "strings"

// ClassifyProvider returns one of:
//   "deepseek" / "openai" / "anthropic" / "qwen" / "ollama" / "other"
// based on a heuristic over (baseURL, protocol). Used for a one-time
// migration to backfill the provider_type column and as a fallback when
// the user hasn't explicitly chosen a type.
func ClassifyProvider(baseURL string, protocol Protocol) string {
	if protocol == ProtocolAnthropic {
		return "anthropic"
	}
	host := strings.ToLower(baseURL)
	switch {
	case strings.Contains(host, "api.deepseek.com"):
		return "deepseek"
	case strings.Contains(host, "api.openai.com"):
		return "openai"
	case strings.Contains(host, "dashscope") || strings.Contains(host, "aliyuncs.com"):
		return "qwen"
	case strings.Contains(host, "127.0.0.1:11434") || strings.Contains(host, "localhost:11434"):
		return "ollama"
	default:
		return "other"
	}
}
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
go test ./service/ -run TestClassifyByBaseURL -v
```
预期：PASS。

- [ ] **Step 5: 在 `service/db.go:migrate()` 加 provider_type 迁移**

在现有 `ALTER TABLE providers ADD COLUMN default_model ...` 那行旁边追加：

```go
	_, _ = db.Exec(`ALTER TABLE providers ADD COLUMN provider_type TEXT NOT NULL DEFAULT ''`)

	// One-time backfill: classify rows whose provider_type is still empty.
	rows, err := db.Query(`SELECT id, base_url, protocol FROM providers WHERE provider_type=''`)
	if err == nil {
		type row struct {
			id       int64
			baseURL  string
			protocol string
		}
		var todo []row
		for rows.Next() {
			var r row
			if err := rows.Scan(&r.id, &r.baseURL, &r.protocol); err == nil {
				todo = append(todo, r)
			}
		}
		rows.Close()
		for _, r := range todo {
			pt := ClassifyProvider(r.baseURL, Protocol(r.protocol))
			_, _ = db.Exec(`UPDATE providers SET provider_type=? WHERE id=?`, pt, r.id)
		}
	}
```

- [ ] **Step 6: 改 Provider struct + 持久化字段**

`service/db.go` 的 Provider struct 加字段：

```go
type Provider struct {
    ID           int64
    UserID       string
    Name         string
    BaseURL      string
    APIKey       string
    Protocol     Protocol
    Enabled      bool
    DefaultModel string
    ProviderType string   // NEW: deepseek|openai|anthropic|qwen|ollama|other
    CreatedAt    time.Time
}
```

`service/provider.go` 的 INSERT/UPSERT/SELECT 全部带上 provider_type 字段（如果用户传了就用，没传就用 ClassifyProvider 自动归类）。具体改动跟着现有 SQL 字段顺序加即可。

- [ ] **Step 7: 跑现有 service 测试不破坏回归**

```bash
go test ./service/ -v
```
预期：无新失败。

- [ ] **Step 8: Commit**

```bash
git add service/provider_classify.go service/provider_classify_test.go service/db.go service/provider.go
git commit -m "feat(ai/service): providers.provider_type column + auto-classify migration"
```

---

## Task 6: Go — model_capability 规则表

**Files:**
- Create: `service/model_capability.go`, `service/model_capability_test.go`

- [ ] **Step 1: 写失败测试**

```go
// service/model_capability_test.go
package service

import "testing"

func TestSupportsThinking(t *testing.T) {
	cases := []struct {
		providerType string
		modelName    string
		want         bool
	}{
		{"deepseek", "deepseek-v4-pro", true},
		{"deepseek", "deepseek-reasoner", true},
		{"deepseek", "anything", true},
		{"anthropic", "claude-3-7-sonnet-20250219", true},
		{"anthropic", "claude-4-sonnet", true},
		{"anthropic", "claude-3-5-sonnet-20241022", false},
		{"anthropic", "claude-3-opus", false},
		{"openai", "o1-preview", true},
		{"openai", "o3-mini", true},
		{"openai", "o4-mini", true},
		{"openai", "gpt-5", true},
		{"openai", "gpt-5-turbo", true},
		{"openai", "gpt-4o", false},
		{"openai", "gpt-4-turbo", false},
		{"qwen", "qwen3-72b", false},
		{"ollama", "llama3", false},
		{"other", "anything", false},
		{"", "", false},
	}
	for _, c := range cases {
		got := SupportsThinking(c.providerType, c.modelName)
		if got != c.want {
			t.Errorf("SupportsThinking(%q,%q)=%v want %v",
				c.providerType, c.modelName, got, c.want)
		}
	}
}
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
go test ./service/ -run TestSupportsThinking -v
```
预期：FAIL（SupportsThinking 未定义）。

- [ ] **Step 3: 实现 `service/model_capability.go`**

```go
package service

import "regexp"

// SupportsThinking reports whether (provider type, model name) supports
// a user-controlled thinking budget. Used by /v1/ai/providers to populate
// the supports_thinking flag the UI uses to enable/disable the thinking bar.
//
// Rules:
//   deepseek  → always true (all DeepSeek models support thinking mode)
//   anthropic → claude-3-7-* and claude-4-* and beyond
//   openai    → o-series (o1/o3/o4) and gpt-5+
//   qwen/ollama/other/empty → false (deferred)
func SupportsThinking(providerType, modelName string) bool {
	switch providerType {
	case "deepseek":
		return true
	case "anthropic":
		return claudeThinkingRe.MatchString(modelName)
	case "openai":
		return openaiThinkingRe.MatchString(modelName)
	}
	return false
}

var (
	claudeThinkingRe = regexp.MustCompile(`^claude-(3-7|4-|5-)`)
	openaiThinkingRe = regexp.MustCompile(`^(o1|o3|o4|gpt-5)`)
)
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
go test ./service/ -run TestSupportsThinking -v
```
预期：PASS。

- [ ] **Step 5: Commit**

```bash
git add service/model_capability.go service/model_capability_test.go
git commit -m "feat(ai/service): model_capability rule table for thinking support"
```

---

## Task 7: Go — /v1/ai/providers 响应附 provider_type + supports_thinking

**Files:**
- Modify: `service/provider.go` 的 list/serialize 方法
- Modify: `route/v2/providers.go` 的 handler 响应结构
- Modify: `route/v2/providers_test.go`（如有）

- [ ] **Step 1: 写测试 / 扩展现有测试**

如果 `route/v2/providers_test.go` 不存在，新建：

```go
// route/v2/providers_test.go
package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func TestProvidersResponseIncludesThinkingFlags(t *testing.T) {
	// Setup: in-memory DB + insert one DeepSeek provider
	svc := newTestSvc(t)
	_, err := svc.Providers().Upsert(testCtx, &service.Provider{
		UserID:       "u1",
		Name:         "DS",
		BaseURL:      "https://api.deepseek.com/v1",
		APIKey:       "k",
		Protocol:     service.ProtocolOpenAI,
		Enabled:      true,
		DefaultModel: "deepseek-v4-pro",
		ProviderType: "deepseek",
	})
	require.NoError(t, err)

	e := echo.New()
	h := NewProvidersHandler(svc)
	req := httptest.NewRequest(http.MethodGet, "/v1/ai/providers", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	require.NoError(t, h.List(c))

	var body []map[string]any
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body))
	require.Len(t, body, 1)
	require.Equal(t, "deepseek", body[0]["provider_type"])
	require.Equal(t, true, body[0]["supports_thinking"])
}
```

> Borrow `newTestSvc` / `testCtx` patterns from existing service tests; if absent, set up minimal fixtures inline.

- [ ] **Step 2: 运行测试，确认失败**

```bash
go test ./route/v2/ -run TestProvidersResponseIncludesThinkingFlags -v
```
预期：FAIL（响应里没有这些字段）。

- [ ] **Step 3: 修改响应结构**

`route/v2/providers.go` 的 list handler，把响应 DTO 改成包含两字段：

```go
type providerDTO struct {
	ID               int64  `json:"id"`
	Name             string `json:"name"`
	BaseURL          string `json:"base_url"`
	Protocol         string `json:"protocol"`
	Enabled          bool   `json:"enabled"`
	DefaultModel     string `json:"default_model"`
	ProviderType     string `json:"provider_type"`
	SupportsThinking bool   `json:"supports_thinking"`
}

func toDTO(p *service.Provider) providerDTO {
	return providerDTO{
		ID:               p.ID,
		Name:             p.Name,
		BaseURL:          p.BaseURL,
		Protocol:         string(p.Protocol),
		Enabled:          p.Enabled,
		DefaultModel:     p.DefaultModel,
		ProviderType:     p.ProviderType,
		SupportsThinking: service.SupportsThinking(p.ProviderType, p.DefaultModel),
	}
}
```

把 List/Get/Upsert 处理器返回时全部走 `toDTO`。

- [ ] **Step 4: 运行测试，确认通过**

```bash
go test ./route/v2/ -run TestProvidersResponseIncludesThinkingFlags -v
```
预期：PASS。

- [ ] **Step 5: Commit**

```bash
git add route/v2/providers.go route/v2/providers_test.go
git commit -m "feat(ai/route): /v1/ai/providers exposes provider_type + supports_thinking"
```

---

## Task 8: Go — Anthropic 通道 ThinkingConfig 化

**Files:**
- Modify: `service/cloud_anthropic.go:84-118` (convertToAnthropic + struct)
- Modify: `service/cloud_anthropic_test.go`
- Modify: `route/v2/chat.go` 调用处 (lines 116-118 area)

- [ ] **Step 1: 扩展测试**

在 `service/cloud_anthropic_test.go` 末尾追加：

```go
func TestConvertToAnthropic_ThinkingConfig(t *testing.T) {
	t.Run("disabled omits thinking", func(t *testing.T) {
		req := OpenAIChatRequest{
			Model: "claude-4-sonnet", MaxTokens: 8000,
			Messages: []OpenAIMessage{{Role: "user", Content: "hi"}},
		}
		ar := ConvertToAnthropicWithThinking(req,
			ThinkingControl{Enabled: false, Level: "medium"})
		require.Nil(t, ar.Thinking)
	})
	t.Run("enabled medium → 8192 budget", func(t *testing.T) {
		req := OpenAIChatRequest{Model: "claude-4-sonnet", MaxTokens: 0,
			Messages: []OpenAIMessage{{Role: "user", Content: "hi"}}}
		ar := ConvertToAnthropicWithThinking(req,
			ThinkingControl{Enabled: true, Level: "medium"})
		require.NotNil(t, ar.Thinking)
		require.Equal(t, "enabled", ar.Thinking.Type)
		require.Equal(t, 8192, ar.Thinking.BudgetTokens)
	})
	for _, c := range []struct {
		level   string
		budget  int
	}{
		{"low", 4096}, {"medium", 8192}, {"high", 16384}, {"max", 32768},
	} {
		t.Run("level="+c.level, func(t *testing.T) {
			req := OpenAIChatRequest{Model: "claude-4-sonnet",
				Messages: []OpenAIMessage{{Role: "user", Content: "hi"}}}
			ar := ConvertToAnthropicWithThinking(req,
				ThinkingControl{Enabled: true, Level: c.level})
			require.Equal(t, c.budget, ar.Thinking.BudgetTokens)
		})
	}
	t.Run("legacy path: no thinking control = no thinking", func(t *testing.T) {
		req := OpenAIChatRequest{Model: "claude-4-sonnet", MaxTokens: 32000}
		ar := convertToAnthropic(req)
		require.Nil(t, ar.Thinking,
			"after refactor, max_tokens alone must NOT auto-enable thinking")
	})
}
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
go test ./service/ -run TestConvertToAnthropic_ThinkingConfig -v
```
预期：FAIL（`ConvertToAnthropicWithThinking` / `ThinkingControl` 未定义；最后一个 case 也会因为旧 max_tokens 自动逻辑而 FAIL）。

- [ ] **Step 3: 重构 `service/cloud_anthropic.go`**

替换 `convertToAnthropic` 函数：

```go
// ThinkingControl is the cross-provider abstraction propagated from the UI.
type ThinkingControl struct {
	Enabled bool
	Level   string // "low" | "medium" | "high" | "max"
}

// ConvertToAnthropicWithThinking converts an OpenAI chat request to Anthropic
// format and applies the user's thinking control as a budget_tokens setting.
//
// Mapping (matches docs/superpowers/specs/2026-04-28-thinking-intensity-design.md §4.2):
//   disabled       → no thinking field
//   low / med / high / max → 4096 / 8192 / 16384 / 32768
func ConvertToAnthropicWithThinking(req OpenAIChatRequest, tc ThinkingControl) AnthropicRequest {
	maxTokens := req.MaxTokens
	if maxTokens == 0 {
		maxTokens = 16000
	}

	var system string
	var messages []AnthropicMessage
	for _, m := range req.Messages {
		if m.Role == "system" {
			system = m.Content
		} else {
			messages = append(messages, AnthropicMessage{Role: m.Role, Content: m.Content})
		}
	}

	ar := AnthropicRequest{
		Model: req.Model, Messages: messages, System: system,
		MaxTokens: maxTokens, Stream: req.Stream,
	}

	if tc.Enabled {
		budget := anthropicBudgetFor(tc.Level)
		ar.Thinking = &AnthropicThinking{Type: "enabled", BudgetTokens: budget}
		// Anthropic requires max_tokens > budget_tokens.
		if ar.MaxTokens <= budget {
			ar.MaxTokens = budget + 1024
		}
	}
	return ar
}

func anthropicBudgetFor(level string) int {
	switch level {
	case "low":
		return 4096
	case "high":
		return 16384
	case "max":
		return 32768
	default: // "medium" or unknown
		return 8192
	}
}

// convertToAnthropic preserves the old signature (no thinking) for callers
// that haven't migrated yet, but no longer auto-enables thinking based on
// max_tokens. UI-driven thinking goes through ConvertToAnthropicWithThinking.
func convertToAnthropic(req OpenAIChatRequest) AnthropicRequest {
	return ConvertToAnthropicWithThinking(req, ThinkingControl{})
}
```

- [ ] **Step 4: 在 `route/v2/chat.go` 调用 Anthropic 适配器处加 thinking 透传**

定位 `case service.ProtocolAnthropic:`（约 116 行），把请求里的 thinking 字段读出来并传给适配器。`OpenAIChatRequest` 现在没有 thinking 字段，先扩展它：

```go
// service/cloud_anthropic.go
type OpenAIChatRequest struct {
	Model     string          `json:"model"`
	Messages  []OpenAIMessage `json:"messages"`
	Stream    bool            `json:"stream"`
	MaxTokens int             `json:"max_tokens,omitempty"`

	// Extended fields used by the gateway to propagate user thinking control
	// when the Python agent forwards them via extra_body / reasoning_effort.
	ReasoningEffort string                 `json:"reasoning_effort,omitempty"`
	ExtraBody       map[string]any         `json:"-"` // populated below
	ThinkingControl *ThinkingControl       `json:"-"` // resolved before convert
}
```

不过更简洁的做法：在 chat.go 解析请求时手动 unmarshal 一遍 `extra_body.thinking` + `reasoning_effort`，组装 ThinkingControl，传给 `ConvertToAnthropicWithThinking`：

```go
// route/v2/chat.go (inside ProtocolAnthropic case)
var raw map[string]json.RawMessage
_ = json.Unmarshal(body, &raw)

tc := service.ThinkingControl{}
// reasoning_effort + extra_body.thinking come from agent.py
if rawEffort, ok := raw["reasoning_effort"]; ok {
	var s string
	_ = json.Unmarshal(rawEffort, &s)
	tc.Level = mapEffortToLevel(s) // helper: "minimal"→disabled, etc.
	tc.Enabled = s != "minimal"
}
if eb, ok := raw["extra_body"]; ok {
	var m map[string]any
	_ = json.Unmarshal(eb, &m)
	if t, ok := m["thinking"].(map[string]any); ok {
		if typ, _ := t["type"].(string); typ == "disabled" {
			tc.Enabled = false
		} else if typ == "enabled" {
			tc.Enabled = true
		}
	}
}

ar := service.ConvertToAnthropicWithThinking(req, tc)
```

加 helper：

```go
func mapEffortToLevel(s string) string {
	switch s {
	case "low": return "low"
	case "medium": return "medium"
	case "high": return "high"
	case "max": return "max"
	}
	return "medium"
}
```

- [ ] **Step 5: 运行测试，确认通过**

```bash
go test ./service/ -run TestConvertToAnthropic -v
```
预期：所有 case PASS。

- [ ] **Step 6: 跑全部 service + route 测试**

```bash
go test ./service/ ./route/... -v
```
预期：无新失败。如果有，多半是别的代码还在调老 `convertToAnthropic` 期望它根据 max_tokens 自动开 thinking — 把那些调用迁移到 `ConvertToAnthropicWithThinking` 并显式传 ThinkingControl。

- [ ] **Step 7: Commit**

```bash
git add service/cloud_anthropic.go service/cloud_anthropic_test.go route/v2/chat.go
git commit -m "refactor(ai/anthropic): drop max_tokens auto-thinking, accept ThinkingControl"
```

---

## Task 9: Frontend — ThinkingBar.vue 组件

**Files:**
- Create: `NimoOS-UI/src/views/AI/Agent/shell/ThinkingBar.vue`

- [ ] **Step 1: 写组件**

```vue
<!-- NimoOS-UI/src/views/AI/Agent/shell/ThinkingBar.vue -->
<template>
  <div class="thinking-bar" :class="{ disabled: !supportsThinking }">
    <span class="icon">💭</span>
    <span class="label">思考</span>
    <label class="toggle">
      <input
        type="checkbox"
        :checked="enabled"
        :disabled="!supportsThinking"
        @change="onToggle($event.target.checked)"
      />
      <span class="track"><span class="thumb" /></span>
    </label>

    <span class="strength-label">强度</span>
    <select
      class="strength-select"
      :value="level"
      :disabled="!supportsThinking || !enabled"
      @change="onLevelChange($event.target.value)"
    >
      <option value="low">低</option>
      <option value="medium">中</option>
      <option value="high">高</option>
      <option value="max">极高</option>
    </select>

    <span v-if="!supportsThinking" class="unsupported-note">
      该模型不支持思考
    </span>
    <span v-else-if="providerNote" class="provider-note">
      {{ providerNote }}
    </span>
  </div>
</template>

<script>
export default {
  name: 'ThinkingBar',
  props: {
    enabled: { type: Boolean, default: true },
    level: { type: String, default: 'medium' },     // low|medium|high|max
    supportsThinking: { type: Boolean, default: false },
    providerType: { type: String, default: '' },    // for tooltip text
  },
  emits: ['update:enabled', 'update:level'],
  computed: {
    providerNote() {
      if (this.providerType === 'deepseek') {
        return 'DeepSeek 上"低/中"以及"高/极高"行为分别相同'
      }
      return ''
    },
  },
  methods: {
    onToggle(v) { this.$emit('update:enabled', v) },
    onLevelChange(v) { this.$emit('update:level', v) },
  },
}
</script>

<style scoped>
.thinking-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  font-size: 13px;
  color: var(--text-secondary, #555);
  border-top: 1px solid var(--border-color, #e0e0e0);
  min-height: 32px;
}
.thinking-bar.disabled {
  opacity: 0.55;
}
.icon { font-size: 14px; }
.label { font-weight: 500; }
.strength-label { margin-left: 8px; }

.toggle { position: relative; display: inline-block; width: 32px; height: 18px; }
.toggle input { opacity: 0; width: 0; height: 0; }
.track {
  position: absolute; inset: 0; background: #ccc; border-radius: 18px;
  transition: background .15s; cursor: pointer;
}
.toggle input:checked + .track { background: var(--accent, #3b82f6); }
.toggle input:disabled + .track { cursor: not-allowed; }
.thumb {
  position: absolute; top: 2px; left: 2px; width: 14px; height: 14px;
  background: white; border-radius: 50%; transition: left .15s;
}
.toggle input:checked + .track .thumb { left: 16px; }

.strength-select {
  padding: 2px 6px; border: 1px solid var(--border-color, #ccc);
  border-radius: 4px; background: var(--bg-secondary, #fff);
  font-size: 13px; color: inherit;
}
.strength-select:disabled { cursor: not-allowed; }

.unsupported-note, .provider-note {
  margin-left: auto; font-size: 12px; color: var(--text-tertiary, #888);
}
</style>
```

- [ ] **Step 2: 运行 dev server，肉眼验证组件渲染**

```bash
cd /home/nimo/nimo_os/NimoOS-UI
npm run dev
```

打开浏览器，临时在某个页面挂载 ThinkingBar 看视觉效果。或者跳过此步骤直接进入 Task 10 的集成。

- [ ] **Step 3: Commit**

```bash
git add src/views/AI/Agent/shell/ThinkingBar.vue
git commit -m "feat(ai/ui): ThinkingBar component with toggle + strength dropdown"
```

---

## Task 10: Frontend — agentStore 思考状态 + API 包装 + 持久化

**Files:**
- Modify: `NimoOS-UI/src/views/AI/Agent/store/agentStore.js` (state + actions)
- Modify: `NimoOS-UI/src/api/ai.js`（或对应的 API 文件）

- [ ] **Step 1: 找到 API 包装文件**

```bash
cd /home/nimo/nimo_os/NimoOS-UI
grep -rn "listProviders\|listModels" src/api/ src/utils/ src/views/AI/ 2>/dev/null | head -20
```

定位实际的 API 函数 (如 `src/api/ai.js`)。

- [ ] **Step 2: 在 API 包装里加 4 个新函数**

```javascript
// src/api/ai.js (在 ai 对象里追加)
async getThinkingDefaults() {
  const r = await fetch('/v1/ai/agent/user-settings/thinking')
  if (!r.ok) throw new Error(`get thinking defaults failed: ${r.status}`)
  return r.json()
},
async putThinkingDefaults(payload) {
  const r = await fetch('/v1/ai/agent/user-settings/thinking', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!r.ok) throw new Error(`put thinking defaults failed: ${r.status}`)
  return r.json()
},
async patchSessionThinking(sessionId, payload) {
  const r = await fetch(`/v1/ai/agent/sessions/${sessionId}/thinking`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!r.ok) throw new Error(`patch session thinking failed: ${r.status}`)
  return r.json()
},
async getSessionThinking(sessionId) {
  // Reuse session GET if it returns thinking_enabled/level. Otherwise add a
  // dedicated endpoint mirroring the PATCH above.
  const r = await fetch(`/v1/ai/agent/sessions/${sessionId}`)
  if (!r.ok) return null
  const s = await r.json()
  if (s.thinking_enabled === null || s.thinking_enabled === undefined) return null
  return { enabled: !!s.thinking_enabled, level: s.thinking_level || 'medium' }
},
```

> 如果现有 GET session 端点不返回 thinking 列，要么扩展它（Python 侧改一行 SELECT），要么在 Task 4 的 PATCH 旁加个 GET。这里假设扩展 GET 简单，把它放进 Task 4 的 commit（或单独 fix）。

- [ ] **Step 3: 扩展 agentStore.js state**

在 state 初始化处（约 20-21 行附近）加：

```javascript
thinking: {
  enabled: true,
  level: 'medium',
  supportsThinking: false,   // 当前选中模型是否支持
  providerType: '',          // 当前选中模型的 provider type
  defaults: { enabled: true, level: 'medium' }, // 全局默认（启动时拉一次）
},
```

- [ ] **Step 4: 增加 actions**

```javascript
async loadThinkingDefaults() {
  try {
    const d = await ai.getThinkingDefaults()
    state.thinking.defaults = d
  } catch { /* keep hard-coded fallback */ }
},

async loadSessionThinking(sessionId) {
  if (!sessionId) return
  let cfg = await ai.getSessionThinking(sessionId)
  if (!cfg) cfg = { ...state.thinking.defaults }
  state.thinking.enabled = cfg.enabled
  state.thinking.level = cfg.level
},

async setThinkingEnabled(enabled) {
  state.thinking.enabled = enabled
  if (state.activeSessionId) {
    await ai.patchSessionThinking(state.activeSessionId, {
      enabled, level: state.thinking.level,
    })
  }
},

async setThinkingLevel(level) {
  state.thinking.level = level
  if (state.activeSessionId) {
    await ai.patchSessionThinking(state.activeSessionId, {
      enabled: state.thinking.enabled, level,
    })
  }
},

updateThinkingForModel() {
  const sel = state.availableModels.find(m => m.key === state.selectedModel)
  if (!sel) {
    state.thinking.supportsThinking = false
    state.thinking.providerType = ''
    return
  }
  state.thinking.supportsThinking = !!sel.supports_thinking
  state.thinking.providerType = sel.provider_type || ''
},
```

- [ ] **Step 5: 修改 `loadAvailableModels`（约 429-489 行）**

把 cloud provider 解析时把 supports_thinking + provider_type 一并塞入：

```javascript
// 原本的: state.availableModels.push({ key, source, displayName, ... })
// 改为:
state.availableModels.push({
  key,
  source: 'cloud',
  displayName: p.default_model,
  providerName: p.name,
  providerId: p.id,
  supports_thinking: !!p.supports_thinking,
  provider_type: p.provider_type || '',
})
```

本地 ollama 模型那段：

```javascript
state.availableModels.push({
  key,
  source: 'local',
  displayName: m.name,
  size: m.size,
  supports_thinking: false,
  provider_type: 'ollama',
})
```

并在 `selectModel` action 末尾追加一行 `actions.updateThinkingForModel()` 让切模型立即更新支持状态。

- [ ] **Step 6: 在 `send` action 里把 thinking 加到 RunRequest**

定位 `runAgentRun(...)` 调用（约 295 行），改：

```javascript
await runAgentRun(
  state.activeSessionId,
  {
    message: text,
    model: modelName,
    thinking: state.thinking.supportsThinking ? {
      enabled: state.thinking.enabled,
      level: state.thinking.level,
    } : null,
  },
  providerType,
  state.abortController.signal,
  actions,
  onError,
)
```

- [ ] **Step 7: Commit**

```bash
git add src/api/ai.js src/views/AI/Agent/store/agentStore.js
git commit -m "feat(ai/ui): agentStore thinking state + persistence + run plumbing"
```

---

## Task 11: Frontend — AgentTopbar 挂载 ThinkingBar

**Files:**
- Modify: `NimoOS-UI/src/views/AI/Agent/shell/AgentTopbar.vue`
- Modify: 父组件（找 AgentTopbar 的 mount 点，把 thinking state + handlers 接进来）

- [ ] **Step 1: 改 AgentTopbar.vue**

```vue
<!-- 在 ModelPicker 下方加一行 ThinkingBar -->
<template>
  <div class="agent-topbar">
    <!-- 原有内容 -->
    <div class="topbar-row">
      <!-- 左侧 sidebar toggle, 标题, 模型选择 -->
      <ModelPicker
        :availableModels="availableModels"
        :selectedKey="selectedModel"
        @select="$emit('select-model', $event)"
        @open-settings="$emit('open-settings')"
      />
      <!-- ... -->
    </div>
    <ThinkingBar
      :enabled="thinking.enabled"
      :level="thinking.level"
      :supportsThinking="thinking.supportsThinking"
      :providerType="thinking.providerType"
      @update:enabled="$emit('thinking-enabled', $event)"
      @update:level="$emit('thinking-level', $event)"
    />
  </div>
</template>

<script>
import ModelPicker from './ModelPicker.vue'
import ThinkingBar from './ThinkingBar.vue'

export default {
  name: 'AgentTopbar',
  components: { ModelPicker, ThinkingBar },
  props: {
    sessionId: String,
    storedTitle: String,
    regeneratingTitleFor: String,
    theme: String,
    rightCollapsed: Boolean,
    availableModels: Array,
    selectedModel: String,
    thinking: {
      type: Object,
      default: () => ({
        enabled: true, level: 'medium',
        supportsThinking: false, providerType: '',
      }),
    },
  },
  emits: [
    'toggle-left', 'toggle-right', 'toggle-theme',
    'select-model', 'regenerate-title', 'open-settings', 'update-title',
    'thinking-enabled', 'thinking-level',
  ],
}
</script>
```

- [ ] **Step 2: 改父组件 `Agent.vue`**

挂载点是 `NimoOS-UI/src/views/AI/Agent/Agent.vue`。在现有 `<AgentTopbar ...>` 标签上追加 props/handlers：

```vue
<AgentTopbar
  :sessionId="..."
  ...其他原有属性
  :thinking="store.state.thinking"
  @thinking-enabled="store.actions.setThinkingEnabled($event)"
  @thinking-level="store.actions.setThinkingLevel($event)"
/>
```

并在 Agent.vue 的 onMounted 或对应初始化钩子中调用 `store.actions.loadThinkingDefaults()` 一次（如果它还没在某个全局入口被调过）；切换 session 时调用 `store.actions.loadSessionThinking(sessionId)` + `store.actions.updateThinkingForModel()`。

- [ ] **Step 3: 跑 dev server，肉眼验证**

```bash
cd /home/nimo/nimo_os/NimoOS-UI
npm run dev
```

预期：选 DeepSeek 模型 → 思考栏可点；切到 Ollama 本地模型 → 思考栏灰显「该模型不支持思考」。

- [ ] **Step 4: Commit**

```bash
git add src/views/AI/Agent/shell/AgentTopbar.vue src/views/AI/Agent/<父组件>.vue
git commit -m "feat(ai/ui): mount ThinkingBar below ModelPicker in AgentTopbar"
```

---

## Task 12: Frontend — Settings 页"默认思考强度"区块

**Files:**
- Create: `NimoOS-UI/src/views/AI/Settings/sections/ThinkingDefaultsSection.vue`
- Modify: Settings 页主组件（把新区块挂上去）

- [ ] **Step 1: 写 ThinkingDefaultsSection.vue**

```vue
<template>
  <section class="settings-section">
    <h2>默认思考强度</h2>
    <div class="card">
      <p class="hint">
        新建会话时使用以下设置作为初始值。不支持思考的模型会自动忽略。
      </p>
      <div class="row">
        <label class="toggle-label">
          <input type="checkbox" v-model="enabled" @change="save" />
          默认开启思考
        </label>
      </div>
      <div class="row">
        <span>默认强度:</span>
        <select v-model="level" :disabled="!enabled" @change="save">
          <option value="low">低</option>
          <option value="medium">中</option>
          <option value="high">高</option>
          <option value="max">极高</option>
        </select>
      </div>
      <p v-if="saving" class="status">保存中…</p>
      <p v-else-if="savedAt" class="status">已保存</p>
    </div>
  </section>
</template>

<script>
import { ai } from '@/api/ai'

export default {
  name: 'ThinkingDefaultsSection',
  data() {
    return { enabled: true, level: 'medium', saving: false, savedAt: 0 }
  },
  async mounted() {
    try {
      const d = await ai.getThinkingDefaults()
      this.enabled = d.enabled
      this.level = d.level
    } catch {}
  },
  methods: {
    async save() {
      this.saving = true
      try {
        await ai.putThinkingDefaults({ enabled: this.enabled, level: this.level })
        this.savedAt = Date.now()
      } finally {
        this.saving = false
      }
    },
  },
}
</script>

<style scoped>
.settings-section { margin-bottom: 24px; }
.card {
  border: 1px solid var(--border-color, #e0e0e0);
  border-radius: 8px; padding: 16px;
}
.hint { color: var(--text-secondary, #777); margin-bottom: 12px; font-size: 13px; }
.row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.toggle-label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
.status { font-size: 12px; color: var(--text-tertiary, #999); }
</style>
```

- [ ] **Step 2: 在 `Settings.vue` 挂载**

主组件路径：`NimoOS-UI/src/views/AI/Settings/Settings.vue`。在现有 `ModelsSection` 之后（或 NavRail 控制的合适分页下）挂载：

```vue
<script>
import ThinkingDefaultsSection from './sections/ThinkingDefaultsSection.vue'
// ...
</script>

<template>
  <!-- 既有内容 -->
  <ModelsSection v-if="activeSection === 'models'" />
  <ThinkingDefaultsSection v-if="activeSection === 'thinking'" />
</template>
```

Settings 页用 `SettingsNavRail.vue` 切区块，需要在 NavRail 里加一项「思考」。改 `SettingsNavRail.vue`，在导航数组里加：`{ key: 'thinking', label: '思考强度', icon: '💭' }`（按现有 NavRail 的数据结构调整字段）。

- [ ] **Step 3: dev server 验证**

```bash
npm run dev
```

预期：进入 Settings，看到「默认思考强度」区块，能切换、改下拉、看到"已保存"提示。

- [ ] **Step 4: Commit**

```bash
git add src/views/AI/Settings/sections/ThinkingDefaultsSection.vue src/views/AI/Settings/<主组件>.vue
git commit -m "feat(ai/ui): settings page section for default thinking level"
```

---

## Task 13: 端到端冒烟测试 + 提交收尾

**Files:** 手动验证清单（不写代码）

按 spec §9 跑一遍：

- [ ] **1. 默认状态**：新建会话，思考栏显示「中」档、enabled=true。后端日志或 DeepSeek API 抓包验证 `reasoning_effort=high`。
- [ ] **2. 关闭思考**：toggle 关，发条消息。响应里没有 `<thinking>` 块；后端发出的请求 `extra_body={"thinking":{"type":"disabled"}}`。
- [ ] **3. 极高档**：切到「极高」，发"算 3 位数乘法"。响应有 thinking 块；多轮带工具调用时 reasoning_content 正确回放（不报 400）。
- [ ] **4. 刷新恢复**：刷新页面，思考栏的 toggle/level 正确恢复成上次设置。
- [ ] **5. 不支持模型**：切到 ollama 本地模型，思考栏灰显「该模型不支持思考」。
- [ ] **6. 全局默认**：进 Settings，把默认改成「低」。新建一个会话，思考栏显示「低」。回到老会话，仍是老会话之前的设置（不被改动）。
- [ ] **7. Anthropic 老路**（如果环境里有 Anthropic provider）：未显式选思考档时，请求里**不**自动开 thinking（验证 cloud_anthropic 重构生效）。
- [ ] **8. Provider type 迁移**：升级前已有的 provider 配置，重启后 `provider_type` 字段被自动归类（用 sqlite3 直接看 providers 表）。

**最终 commit（如果手动验证发现小 bug 顺手修了）：**

```bash
git status
git add -p   # 把零散修复 stage 进去
git commit -m "fix(ai): smoke-test polish for thinking selector"
```

---

## Self-review 检查清单

实现这个计划时若发现以下问题，直接在原任务里修：

1. **Spec 覆盖**：spec §4(数据模型) → Task 1; spec §5(API) → Task 3+4+10; spec §6(UI) → Task 9+11+12; spec §7(架构) → Task 2+3+5+6+7+8; spec §8(边界) → Task 8 重构 + Task 13 冒烟。无遗漏。
2. **Anthropic 路径**：Task 8 假设 chat.go 走 OpenAI→Anthropic 转换。如果实际链路是 Python 直接到 Go 的 anthropic 适配器（route/v2/chat.go ProtocolAnthropic 分支），重读那段代码确认入参 body 是 OpenAI 格式 + extra_body。
3. **SDK 版本兼容**：Task 3 假设 `OpenAIChatCompletionsModel` 接受 `model_settings` kwarg。requirements.txt 里 `openai-agents>=0.0.19`，需查文档确认；不行就走 `Agent(..., model_settings=...)` 路径。
4. **GET session 端点**：Task 10 的 `getSessionThinking` 假设 GET /agent/sessions/{id} 返回 thinking 列。如果不返回，去 Task 4 加一行 SELECT 扩展 GET，或单独建 GET /agent/sessions/{id}/thinking。
5. **API base path**：Task 10 写的 `/v1/ai/agent/user-settings/thinking` 等路径需对照实际 Go 路由前缀（route/v2/agent.go 的 mount 路径）。如果 Python 端点是 `/agent/...` 而 Go 没代理这条路径，需在 route/v2/agent.go 加转发。

---

## 完成后的 Definition of Done

- 所有 Go 测试 PASS：`go test ./service/... ./route/...`
- 所有 Python 测试 PASS：`pytest agent/tests/`
- 前端 `npm run build` 无新告警
- §13 8 项手动冒烟全过
- 提交历史干净（每个 Task 一个 commit，必要时 fix 小 commit 跟在后面）
