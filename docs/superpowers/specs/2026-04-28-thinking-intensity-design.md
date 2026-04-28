# 模型思考强度选择 — 设计文档

- **日期**：2026-04-28
- **范围**：在 AI Agent 页面的模型选择旁，新增"思考开关 + 强度选择"，前后端打通；首期接 DeepSeek，预留 Anthropic / OpenAI 扩展点。
- **作者**：haowen.lei

---

## 1. 背景与现状

NimoOS-AI 当前 thinking 行为：

- **Anthropic 通道**（`service/cloud_anthropic.go`）：硬编码逻辑——只要 `max_tokens >= 16000` 就自动启用 `thinking`，`budget_tokens` 写死 8000。用户不可控。
- **DeepSeek 通道**（OpenAI-compatible）：默认 `thinking.enabled=true, effort=high`（DeepSeek 服务端默认值），客户端不传任何控制参数。用户不可控。
- **Agents SDK** 已经接好 `should_replay_reasoning_content` 钩子 + `_inject_synthetic_reasoning` 占位（`agent/agent.py`），DeepSeek thinking 历史回放不再 400。
- **前端**（`NimoOS-UI/src/views/AI/Agent/shell/ModelPicker.vue`）：只有模型下拉，无任何思考相关控件。

本设计在不破坏上述基础设施的前提下，让用户**显式控制思考开关与强度**。

## 2. 目标

1. 用户在 AI Agent 页面能为当前会话独立设置「思考开关」与「思考强度」。
2. 设计统一刻度（4 档），后端各 provider 适配器负责翻译为各自原生参数。
3. 首期适配 DeepSeek；Anthropic / OpenAI 适配器同步预留接口（Anthropic 顺手把硬编码改掉）。
4. 不支持思考的模型在 UI 上明确灰显标注「该模型不支持思考」，但不隐藏控件。
5. 全局默认值可在设置页配置。

## 3. 非目标

- 不做"按模型粘性"记忆（per-model 持久化）。仅 per-session + 全局默认。
- 不在本期暴露 `temperature`/`top_p` 等 sampling 参数。
- 不做 Ollama 本地模型的思考能力探测（`supports_thinking` 全 false）。
- 不改动 reasoning_content 历史回放逻辑。

## 4. 数据模型

### 4.1 统一枚举

```ts
type ThinkingLevel = "low" | "medium" | "high" | "max";

interface ThinkingConfig {
  enabled: boolean;       // 总开关
  level: ThinkingLevel;   // enabled=false 时仍保留，便于再开启时恢复
}
```

### 4.2 各 provider 映射表

| 统一值 | DeepSeek | Anthropic (`thinking.budget_tokens`) | OpenAI (`reasoning_effort`) |
|---|---|---|---|
| `enabled=false` | `extra_body={"thinking":{"type":"disabled"}}` | 不传 thinking 字段 | `reasoning_effort="minimal"` |
| `low` | `effort=high`（DeepSeek 内部映射） | 4096 | `low` |
| `medium` | `effort=high` | 8192 | `medium` |
| `high` | `effort=max` | 16384 | `high` |
| `max` | `effort=max`（DeepSeek 上限） | 32768 | `high`（OpenAI 无 max 档，复用 high） |

> DeepSeek 上 `low/medium` 行为相同（API 文档明确 low/medium → high），`high/max` 行为相同（API 文档：xhigh → max）。这是文档侧约束，不是缺陷。UI 通过模型相关 tooltip 提示用户。
> OpenAI 没有真正的"关闭"——`reasoning_effort="minimal"` 是最接近的近似（推理模型必然推理）。

### 4.3 数据库变更

`sessions` 表新增两列：

```sql
ALTER TABLE sessions ADD COLUMN thinking_enabled INTEGER;  -- 0/1, NULL 表示用全局默认
ALTER TABLE sessions ADD COLUMN thinking_level TEXT;        -- 'low'|'medium'|'high'|'max', NULL 表示用全局默认
```

全局默认配置（已有 settings 存储里加两项）：

- `default_thinking_enabled` (默认 `true`)
- `default_thinking_level` (默认 `"medium"`)

老会话所有列为 NULL → 自动 fallback 全局默认，不需要数据回填。

## 5. API 协议

### 5.1 RunRequest 新增 thinking 字段

`POST /v1/ai/agent/sessions/{id}/run` 请求体：

```json
{
  "message": "...",
  "model": "...",
  "kind": "chat",
  "thinking": { "enabled": true, "level": "medium" }
}
```

- `thinking` 字段可缺省。
- 后端按以下顺序合并：请求体 → session 表 → 全局默认。
- 老版本 UI 不传字段时，行为与升级前等价（用全局默认）。

### 5.2 Provider 元数据

`GET /v1/ai/providers` 响应每项追加：

```json
{
  "id": "...",
  "default_model": "...",
  "supports_thinking": true,
  "provider_type": "deepseek"
}
```

`GET /v1/ai/models`（本地 Ollama）每项追加 `supports_thinking: false`（首期全为 false）。

### 5.3 Session 配置读写

新增端点（或复用现有 session 更新端点）：

- `PATCH /v1/ai/agent/sessions/{id}` 支持更新 `thinking_enabled` / `thinking_level`。
- 用户在思考栏切换 toggle / 强度时立即 PATCH。

### 5.4 全局默认配置

设置页通过现有的全局配置接口读写 `default_thinking_enabled` / `default_thinking_level`。

## 6. UI 设计

### 6.1 思考栏（位置：ModelPicker 下方一行）

```
┌─ ModelPicker (现有下拉) ──────┐
│ 🤖 DeepSeek v4 Pro          ▼ │
└───────────────────────────────┘
┌─ 思考栏 (新增) ───────────────┐
│ 💭 思考  [●○ 开]  强度 [中 ▾] │
└───────────────────────────────┘
```

- 模型支持思考时：思考栏正常显示，可交互。
- 模型不支持思考时：思考栏灰显，标注「该模型不支持思考」，控件不可点击；当前 session 已设置的 thinking 配置保留（不被擦除）但不生效。
- 思考栏始终占位（高度恒定），切模型只在"可交互"和"灰显"两态之间切换，不影响上下布局。

### 6.2 强度下拉的选项

```
低     - 快速浅思考
中     - 标准 (默认)
高     - 深度思考
极高   - 最长思考
```

下方 tooltip 根据当前 provider 显示差异提示，例如 DeepSeek：

> 当前 DeepSeek 模型上"低/中"以及"高/极高"行为分别相同。

### 6.3 设置页新增「默认思考强度」区块

`AI/Settings` 页：

```
默认思考强度
─────────────
[●○ 默认开启思考]
默认强度: [中 ▾]

新建会话时使用以上设置作为初始值。不支持思考的模型会自动忽略。
```

### 6.4 状态流

新建会话：

1. `session.thinking_enabled` / `thinking_level` 都是 NULL。
2. UI 读取 → fallback 到全局默认。
3. 用户改 → PATCH 写入 session 表（NULL → 实际值）。
4. 后续切模型保留 session 级配置；刷新页面正确恢复。

## 7. 后端架构

### 7.1 数据流

```
UI ──RunRequest{thinking}──▶ Go gateway
                              │
                              ▼
                    解析 + 合并 thinking 配置
                    (req body → session 表 → 全局默认)
                              │
                              ▼
                    转发到 Python /run
                    (透传 thinking 字段 + provider_type header)
                              │
                              ▼
                    Python AgentRunner.run
                              │
                              ▼
                    根据 provider_type 构造
                    ModelSettings(extra_args/extra_body)
                              │
                              ▼
                    OpenAIChatCompletionsModel
                    透传到 chat.completions.create
                              │
                              ▼
                       DeepSeek/Anthropic/OpenAI API
```

### 7.2 Go 侧改动

- `service/model_capability.go`（新文件）：规则表 + `SupportsThinking(providerType, modelName) bool`。

  ```
  规则:
    deepseek:  全部支持
    anthropic: claude-(3-7|4-) 前缀支持，老 3.5/3.0 不支持
    openai:    ^(o1|o3|o4|gpt-5) 前缀支持，其他（gpt-4o 等）不支持
    qwen/other/ollama: 默认 false
  ```

- `service/provider.go`：`/v1/ai/providers` 响应附 `supports_thinking` 与 `provider_type`。
- `service/session.go`（或对应文件）：sessions 表 schema migration；新增 PATCH 字段支持。
- `service/agent.go`（转发层）：解析 RunRequest.thinking，合并三层来源，附带 provider_type 转发到 Python。
- `service/cloud_anthropic.go`：删除"max_tokens>=16000 自动 thinking"硬编码，改为读 ThinkingConfig。
- `service/settings.go`：新增 `default_thinking_enabled` / `default_thinking_level` 全局配置项。

### 7.3 Python 侧改动

- `agent/main.py`：`RunRequest` model 加 `thinking: Optional[ThinkingConfig]` 字段。
- `agent/agent.py` `AgentRunner.run`：接收 `thinking` 与 `provider_type` 参数，构造 `ModelSettings`。
- `agent/provider_adapters.py`（新文件）：

  ```python
  def build_model_settings(provider_type: str, thinking: ThinkingConfig) -> ModelSettings:
      if provider_type == "deepseek":
          ...  # extra_body + extra_args
      elif provider_type == "anthropic":
          ...
      elif provider_type == "openai":
          ...
      else:
          return ModelSettings()  # 无 thinking 控制
  ```

### 7.4 前端改动

- `NimoOS-UI/src/views/AI/Agent/shell/ThinkingBar.vue`（新组件）：toggle + 强度下拉 + 不支持提示。
- `NimoOS-UI/src/views/AI/Agent/shell/AgentTopbar.vue`：在 ModelPicker 下方挂载 ThinkingBar。
- `NimoOS-UI/src/views/AI/Agent/store/agentStore.js`：thinking 状态、PATCH 持久化、随模型切换更新可用性。
- `NimoOS-UI/src/views/AI/Settings/sections/ThinkingDefaultsSection.vue`（新文件）：全局默认设置区块。

### 7.5 Provider type 细分迁移

现有 provider 配置只有"cloud"粗粒度类型。本次细分为 `openai` / `deepseek` / `qwen` / `anthropic` / `other`：

- 上线 migration 按 `base_url` 启发式自动归类：
  - `api.deepseek.com` → `deepseek`
  - `api.openai.com` → `openai`
  - `dashscope` 域名 → `qwen`
  - Anthropic 通道（已通过 `cloud_anthropic.go` 走 native 协议）→ `anthropic`
  - 其他 → `other`
- 用户可在 provider 设置页手动改类型。
- `other` 类型 = 不传任何 thinking 参数（兼容默认）。

## 8. 边界与异常

### 8.1 DeepSeek 特殊行为

- 思考模式下 sampling 参数（`temperature` / `top_p` / `presence_penalty` / `frequency_penalty`）被 DeepSeek 静默忽略——当前代码未传，无影响。
- `reasoning_content` 历史回放：现有 SDK 钩子 + 合成占位逻辑覆盖，无需改动。
- 关闭思考后再开启：历史里的 `reasoning_content` 仍按现有逻辑回放（DeepSeek 文档：未启用思考时该字段被忽略，安全）。

### 8.2 不支持模型被传 thinking

- 后端 warn log，按"不发送 thinking 参数"处理，不 500。
- UI 已用 `supports_thinking` 阻止，是双重保险。

### 8.3 Provider 报错传播

- DeepSeek/Anthropic/OpenAI 返回的 thinking 相关 400 错误，按现有 error event 流透出给用户。
- 错误信息保留 provider 原文，便于调试。

## 9. 测试要点

最小验证清单：

1. 新建会话默认状态：UI 显示「中」档思考，DeepSeek 实际走 `effort=high`。
2. 关闭思考开关 → DeepSeek `extra_body` 传 `thinking.disabled`，无 thinking 块输出。
3. 切到「极高」→ DeepSeek `effort=max`；带工具调用的多轮对话中 `reasoning_content` 正确回放。
4. 同一会话刷新页面 → session 级思考配置正确恢复。
5. 切到不支持模型（gpt-4o）→ 思考栏灰显「该模型不支持思考」，已设置的 session 配置保留但不生效。
6. 设置页改全局默认 → 新建会话生效，已存在会话不受影响。
7. Anthropic 通道：旧的「max_tokens>=16000 自动 thinking」彻底移除后，未显式设 thinking 的请求行为与升级前的"未自动开启"等价（即不开 thinking）。
8. Provider type migration：现有 provider 配置升级后 `provider_type` 字段正确归类。

## 10. 后续扩展

- **per-model 粘性记忆**（C 方案）：未来可作为可选偏好，不强制。
- **provider 原生档位文案**（最初讨论的 C 方案）：当 OpenAI / Anthropic 接入后，UI 强度下拉可按当前 provider 改文案（如 Anthropic 显示 `4k / 8k / 16k / 32k`）。
- **Ollama 本地模型思考探测**：等 Ollama 上游统一 reasoning 协议后再做。
- **`max` 档位扩展**：DeepSeek/OpenAI 升级新档位时，只动适配器层不动协议。
