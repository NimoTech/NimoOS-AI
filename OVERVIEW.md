# NimoOS-AI

NimoOS 的 AI 服务,为系统提供 **LLM 推理网关** + **本地化 Agent 运行时**。当前版本 `v0.1.0`(alpha)。

绑定 localhost、由 Gateway 转发,API 前缀 `/v1/ai`。

---

## 双进程架构

NimoOS-AI 由两个独立的系统进程组成:

```
                外部请求(Gateway 转发,仅 /v1/ai/*)
                            │
                            ▼
           ┌────────────────────────────────────┐
           │   nimoos-ai.service (Go,Echo)     │
           │   - LLM 推理路由(local / cloud)  │
           │   - Provider/Model/Session 管理   │
           │   - 隐私策略 + 硬黑名单           │
           │   - Master Key 加密 API Key       │
           │   - 反向代理 /v1/ai/agent/*      │
           └────────────────────────────────────┘
                  │                    │
        ┌─────────▼──────────┐  ┌──────▼──────────────┐
        │  本地 Ollama       │  │  nimoos-agent       │
        │  127.0.0.1:11434   │  │  127.0.0.1:8282     │
        │  (系统已装)        │  │  (Python/FastAPI)   │
        └────────────────────┘  └─────────────────────┘
                                          │
                                  云端 LLM(OpenAI/
                                  Anthropic/DeepSeek/
                                  Qwen 等)
```

- `nimoos-ai.service` — Go 二进制,主入口,持有 SQLite 状态。
- `nimoos-agent.service` — Python(FastAPI + openai-agents SDK),跑工具调用型 Agent,主目录 `agent/`。

二者通过本地 HTTP 通信,前端通过 `/v1/ai/agent/*` 经 Go 服务反代到 Python Agent。

---

## API 路由(`/v1/ai`)

| Method | Path | 用途 |
|---|---|---|
| POST | `/chat/completions` | OpenAI 兼容推理(由 Router 决定 local/cloud) |
| POST | `/messages` | Anthropic 兼容推理 |
| GET / POST / PUT / DELETE | `/providers[/:id]` | 用户级云 Provider CRUD |
| GET / PUT | `/policy` | 隐私策略(是否允许云、默认后端、升级确认) |
| GET / POST / DELETE | `/models[/:name]` | 本地模型列表/拉取/删除(Ollama) |
| POST | `/models/pull` | 触发 `ollama pull` |
| GET | `/models/hf/search` | 搜索 Hugging Face |
| GET | `/models/hf/files` | 列模型文件 |
| POST | `/models/hf/import` | 从 HF 导入到本地 |
| GET / POST / DELETE / PATCH | `/sessions[/...]` | 聊天会话与消息持久化 |
| GET | `/services/status` | Ollama + Agent 健康状态 |
| GET | `/agent/health` | Agent 健康检查 |
| ANY | `/agent/*` | 反代到 Python Agent(`:8282`) |
| GET | `/fs/mounts` | 列出 Agent 可访问的挂载点(picker scope) |
| GET / POST / DELETE | `/blacklist` | 硬黑名单(AI 永不可见的路径/模式) |
| GET / POST / DELETE | `/mcp-tokens[/:id]` | 对外 MCP server 的长效 token 管理(JWT 保护) |
| POST | `/mcp-rpc/` | **对外 MCP server** 数据端点(JSON-RPC over Streamable-HTTP)。**JWT 豁免**,改由 Python 侧 Bearer token 校验 → user_id。详见下文「对外 MCP server」 |

**鉴权**:除 `/mcp-rpc/` 外所有路由强制 JWT 校验,**无 localhost 豁免**。这是有意为之 — 防止本机其他进程读到其他用户的云 API Key。校验后注入 `X-NimoOS-User-ID` / `X-NimoOS-User-Name` Header。`/mcp-rpc/` 是唯一例外:它面向外部 AI agent,用长效 MCP token 自行鉴权(见下)。

CORS 暴露的自定义 Header:`X-NimoOS-Force-Cloud`、`X-User-Id`、`X-User-Name`、`X-Agent-Provider-Key/Url/Type`。

---

## 推理路由策略(Router)

`service/router.go` 根据用户策略 + 请求 Header 决定后端:

```
PrivacyPolicy {
  AllowRemote:      bool   # false => 锁死在本地
  DefaultBackend:   "local" | "cloud"
  EscalationPrompt: bool   # 升级到云时是否提示用户
}
```

| `AllowRemote` | `X-NimoOS-Force-Cloud` | `DefaultBackend` | 结果 |
|---|---|---|---|
| false | true | * | **拒绝**(`ErrRemoteNotAllowed`) |
| false | false | * | local(`ForceLocal=true`) |
| true | true | * | cloud |
| true | false | cloud | cloud |
| true | false | local | local |

---

## Provider 自动分类

`service/provider_classify.go` 用 `(baseURL, protocol)` 启发式分类为:
`deepseek` / `openai` / `anthropic` / `qwen` / `ollama` / `other`,用于 UI 展示与一次性的 `provider_type` 回填迁移。

---

## 关键模块

| 包 | 职责 |
|---|---|
| `main.go` | 启动、绑定随机端口、写 `ai.url`、向 Gateway 注册路由、启 Ollama 健康巡检、systemd notify |
| `route/` | Echo 路由,JWT 中间件 |
| `route/v2/` | 各端点处理器(chat / providers / policy / models / sessions / agent / fs / blacklist) |
| `service/` | 业务层(DB 迁移、路由决策、provider/model/session 管理、Ollama 健康) |
| `pkg/config/` | Viper INI 配置加载 |
| `pkg/crypto/` | Master Key(AES-GCM)用于加密 Provider API Key |
| `common/` | 常量(端口、API 前缀、事件类型) |
| `agent/` | Python Agent(FastAPI + openai-agents SDK),含 fs 工具、skills、confirm 机制 |
| `build/sysroot/` | 安装产物:systemd unit、配置样例、setup/cleanup 脚本 |

---

## 数据存储

```
/etc/nimoos/ai.conf             # 配置(INI)
/etc/nimoos/ai_master.key       # AES-GCM 主密钥
/var/lib/nimoos/ai/
  ├── ai.db                     # SQLite:providers / privacy_policies /
  │                             #          chat_sessions / chat_messages /
  │                             #          models / hard_blacklist
  ├── models/                   # 模型相关数据
  └── agent/
      ├── agent.db              # Python Agent 自己的 SQLite
      ├── snapshots/            # Agent 操作快照
      └── venv/                 # Python 虚拟环境
/var/log/nimoos/                # 日志(zap → 文件,systemd 也走 journal)
/var/run/nimoos/ai.url          # 服务发现地址
```

### SQLite 表结构

| 表 | 用途 |
|---|---|
| `providers` | 每个用户的云 Provider(name / base_url / api_key 加密 / protocol / provider_type / default_model) |
| `privacy_policies` | 每用户隐私策略 |
| `chat_sessions` + `chat_messages` | 聊天历史 |
| `models` | 本地模型清单(来源 ollama / huggingface) |
| `hard_blacklist` | 用户级硬黑名单(Agent 永不可访问的路径/模式) |

`provider_type` 列上有幂等 `ALTER TABLE` + 一次性回填迁移逻辑。

---

## 配置样例(`build/sysroot/etc/nimoos/ai.conf.sample`)

```ini
[common]
RuntimePath = /var/run/nimoos
DataPath = /var/lib/nimoos/ai
LogPath = /var/log/nimoos

[ai]
MasterKeyPath = /etc/nimoos/ai_master.key

[agent]
AgentURL = http://127.0.0.1:8282
AgentTimeout = 60
OllamaURL = http://127.0.0.1:11434
```

启动时若 `/etc/nimoos/ai.conf` 不存在,会用上述样例写入一份。

---

## Python Agent(`agent/`)

独立 systemd 单元 `nimoos-agent.service`,跑在 `:8282`,由 `nimoos-ai` 反代。

| 文件 | 用途 |
|---|---|
| `main.py` | FastAPI 应用,流式响应、确认、快照 |
| `agent.py` | `AgentRunner` — 调用 openai-agents SDK |
| `confirm.py` | 工具调用前的人工确认管理 |
| `db.py` | Python 侧 SQLite(`agent.db`) |
| `fs/` | 文件系统工具(paths / ignore / staging / snapshots) |
| `skills/` | 预置技能 |
| `provider_adapters.py` | 适配前端传来的 Provider 配置 |
| `title_gen.py` | 会话标题自动生成 |
| `run_sink.py` | 运行事件落库与回放 |
| `mcp_tokens.py` | 对外 MCP token 的 SQLite 存取(哈希存储、`last_used_at` 节流写) |
| `mcp_server/` | 对外 MCP server(协议适配 + 工具白名单 + 路径门控,见下文) |

依赖见 `requirements.txt`:`fastapi`、`uvicorn`、`openai-agents`、`openai`、`httpx`、`pathspec`、`mcp`(MCP SDK)。

Agent 通过 HTTP Header 接收用户黑名单(`X-Agent-User-Blacklist`,base64+JSON),与 Go 侧硬黑名单配合形成两层过滤。

---

## 对外 MCP server(供外部 AI agent)

把本 NAS 的只读知识能力反向暴露为 **MCP 工具**,供外部 AI agent(Claude Desktop / Cursor 等)通过 MCP 协议调用。与 `mcp_client`(NimoOS 作 MCP 客户端去连别人)方向相反。代码在 `agent/mcp_server/`。

**端点与传输**:外部地址 `http://<nas>/v1/ai/mcp-rpc/`(Go `nimoos-ai` 反代到 Python `:8282` 的 `/mcp-rpc/`)。用官方 `mcp` SDK 的 **Streamable-HTTP 单端点**(`stateless=True` + `json_response=True`);裸路径与带斜杠均返 200(`server.py` 用 raw-ASGI Route 规避 Starlette Mount 的 307 重定向)。

**鉴权(方案 H)**:长效 token,`Authorization: Bearer nimoos_mcp_...`。
- token 存 `agent.db` 的 `mcp_tokens` 表(**只存哈希**;`last_used_at` 60s 节流写)。
- 管理端点 `/v1/ai/mcp-tokens`(GET/POST/DELETE,**JWT 保护**,注入 `X-NimoOS-User-ID`);创建返回的明文 token 仅此一次。UI 在 `/#/ai/settings?section=mcptokens`。
- 数据端点 `/mcp-rpc/` **JWT 豁免**,由 Python 侧校验 Bearer token → 解析出 `user_id`,经 `USER_ID_VAR` 传给工具;后续对 Search/Wiki/Photos 的调用用 `localhost + X-NimoOS-User-ID`(选 H 而非 Go 签发内部 JWT,避免新增特权端点 + 刷新机制)。

**工具白名单(当前 9 个,全只读;写工具永不暴露)**:`nimoos_search`、`read_document`(`file_id` XOR `path`)、`read_file_chunk`、`view_document_page`(把文档某页渲染成 PNG,以 MCP **ImageContent** 返回,交客户端自己的视觉模型看,无服务端 vision)、`wiki_get_node`、`wiki_list_full_tree`、`wiki_recent_changes`、`search_photos`、`list_albums`。

**路径门控**:无头 MCP 无聊天 session,不能用 chat 的 `visible_resources` 门控。`mcp_server/fs_gate.py` 提供 deny-only 门控 `mcp_resolve_read_path()`:`realpath` 防 `..`/符号链接/前缀兄弟(`/DATA-evil`),只放 `/DATA`、挖掉 `/DATA/.system_data`;越界抛 `McpPathDenied`。硬失败(门控拒/渲染失败/超时/参数互斥)统一抛 `McpToolError` → `server._call` 映射为 `CallToolResult(isError=True)`。

**模块**:`server.py`(ASGI/协议适配 + `_call` + `render_result`)、`tools.py`(白名单 `TOOL_SPECS` + 各 `_h_*` handler + `ImageResult`/`McpToolError`)、`fs_gate.py`(路径门控)。设计/计划见 `nimo_os_docs/docs/superpowers/specs/2026-06-*-nimoos-mcp-server-*` 及 `2026-06-30-mcp-*`、`2026-07-01-mcp-token-ui-*`。

**caveat**:Photos 数据层当前无 per-user filter → 多用户下 `search_photos`/`list_albums` 暂不能宣称用户隔离(单用户 NAS 无影响)。

---

## 启动顺序与依赖

systemd 依赖关系(见 `build/sysroot/usr/lib/systemd/system/nimoos-ai.service`):

```
nimoos-gateway.service ──┐
                         ├──▶ nimoos-ai.service ──▶ nimoos-agent.service
nimoos-message-bus.service ┘
```

`nimoos-ai` 用 `Type=notify`,`SdNotify(Ready)` 后才视为启动完成。

外部依赖(系统装好):
- **Ollama**(`:11434`):由 `OllamaChecker` 巡检,3 次连续失败发 `AI:OllamaUnhealthy` 事件,恢复后发 `AI:OllamaRecovered`。
- **GCC + CGO**:`go-sqlite3` 需要 `CGO_ENABLED=1`,与 NimoOS 核心一致。
- **Python venv**:Agent 的 venv 安装到 `/var/lib/nimoos/ai/agent/venv/`,由 `build/scripts/setup/service.d/ai/` 下的 setup 脚本部署。

---

## 构建与部署

```bash
# Go 服务
cd NimoOS-AI && CGO_ENABLED=1 go build -o nimoos-ai .

# 多架构发布(amd64 + arm64)
goreleaser release --snapshot --clean

# 一键安装脚本(系统级,含 Python venv + systemd)
bash nimo_os_docs/scripts/install-ai.sh

# 启动 Python Agent(开发态)
bash nimo_os_docs/scripts/start-ai.sh
```

参见仓库根 `nimo_os_docs/scripts/`:`install-ai.sh`、`start-ai.sh`、`deploy-agent.sh`。

---

## 与其他服务的关系

- **依赖 UserService**:从 `/var/run/nimoos/` 取公钥校验 JWT。
- **依赖 Gateway**:启动时通过 `POST /v1/gateway/routes` 注册 `/v1/ai` 和 `/doc/v1/ai`。
- **依赖 MessageBus**(规划/事件常量已声明):`AI:OllamaUnhealthy` / `AI:OllamaRecovered` 推送到前端。
- **被 UI 调用**:NimoOS-UI 的 AI 对话/设置面板。

---

## 设计要点

1. **API Key 加密**:Master Key 落盘到 `/etc/nimoos/ai_master.key`(0600),Provider 表中 `api_key` 用其做 AES-GCM 加密 + Base64。
2. **无 localhost 豁免**:与其他服务不同,AI 服务即使来自本机也强制 JWT,因为它代理别人的云密钥。
3. **硬黑名单分层**:Go 侧管理用户级黑名单(SQLite),Agent 启动每个会话时把当前用户的黑名单通过 Header 注入 Python 侧,Python 侧再叠加 `pathspec` 做文件忽略。
4. **路由决策与 UI 解耦**:UI 只发请求,`X-NimoOS-Force-Cloud` 由 UI 上的"升级到云"按钮触发;最终允许与否由后端 Policy 决定。

---

## Skills (v2)

Skill bundles live at:
- `/var/lib/nimoos/skills/builtin/<id>/` — read-only, seeded from `//go:embed` on service start.
- `/var/lib/nimoos/skills/users/<uid>/<id>/` — user-writable.
- `/var/lib/nimoos/skills/.runtime/<uid>/` — per-user symlink view, ro-bind-mounted into bwrap at `/skill` for every agent run.

Each bundle is a directory containing:
- `manifest.json` — metadata (id, name, trigger, color, icon, description, examples, version, author).
- `SKILL.md` — instructions the LLM reads to use the skill (capped at 50 KiB).
- `scripts/` — optional executable scripts.
- `resources/` — optional supporting files.

LLM-visible tool: `list_skills()` returns the user's enabled skills (manual-trigger skills are hidden from the list and surface only via UI "Try in chat" injection).

REST endpoints (`/v1/ai/skills/*`):
- `GET /` — list all skills with state overlay
- `POST /` — create a user skill (simple-form JSON; tar.gz upload deferred to v2)
- `GET /:id` — get a single skill
- `PATCH /:id` — toggle enabled (only field accepted)
- `DELETE /:id` — uninstall built-in or delete user skill
- `GET /:id/files/*` — read a single file inside a bundle (symlink-escape safe)
- `GET /:id/export` — download bundle as tar.gz
- `POST /:id/test` — streaming SSE sandbox run (proxies to Python `/agent/sandbox-run`)

Sandbox testing: `POST /v1/ai/skills/<id>/test` proxies to the Python agent's `POST /agent/sandbox-run` which runs a one-shot, no-DB session inside a fresh tmpfs sandbox with `--unshare-net`. The bundle is ro-mounted at `/skill/<id>/`. Per-request ContextVars (`SANDBOX_SKILLS_VAR`, `SANDBOX_SHELL_ROOT_VAR`) keep concurrent runs isolated.

Bundle path safety: `ValidateSkillID` allows `[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$`. `ReadFile` resolves symlinks with `EvalSymlinks` and opens with `O_NOFOLLOW`. The X-Skill-Id header (used by "Try in chat") is regex-validated on the Python side before any path-join.
