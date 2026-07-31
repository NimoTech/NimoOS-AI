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
| ANY | `/_internal/*` | 内部端点(chat/models/mcp runtime/**agent/provider-credentials**),**JWT 豁免 + LocalhostOnly**(`route/middleware.go`)。其中 `GET /_internal/agent/provider-credentials` 返回解密后的云密钥,额外要求 `X-Internal-Token` 共享密钥(见「Channels」) |

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
/var/run/nimoos/ai_internal.token  # 启动时随机生成(0600),/_internal 秘密端点的共享密钥
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
| `channels/` | Telegram / Discord 聊天平台接入(adapter/router/manager/driver,见下文「Channels」) |
| `attachments/` | 会话附件层(ingest / paths / extract / gc);channels 入站文件经 `ingest_external` 注册 |
| `phoenix_tracing.py` | 可选 Phoenix(OTLP)tracing,开关门控(见下文「Phoenix tracing」) |
| `observability/` | `phoenix_compose.yaml` — Phoenix 容器的 NimoOS 应用 compose 清单 |

依赖见 `agent/requirements.txt`:`fastapi`、`uvicorn`、`openai-agents`、`openai`、`httpx`、`pathspec`、`mcp`(MCP SDK)、`discord.py`(Discord channel)、`openinference-instrumentation-openai-agents` + `opentelemetry-sdk/-exporter-otlp-proto-http`(Phoenix tracing)、`pypdf`/`python-docx`/`openpyxl`/`python-pptx`(附件抽取)。

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

**模块**:`server.py`(ASGI/协议适配 + `_call` + `render_result`)、`tools.py`(白名单 `TOOL_SPECS` + 各 `_h_*` handler + `ImageResult`/`McpToolError`)、`fs_gate.py`(路径门控)。设计/计划见 内部设计稿 `2026-06-*-nimoos-mcp-server-*` 及 `2026-06-30-mcp-*`、`2026-07-01-mcp-token-ui-*`。

**caveat**:Photos 数据层当前无 per-user filter → 多用户下 `search_photos`/`list_albums` 暂不能宣称用户隔离(单用户 NAS 无影响)。

---

## Channels(Telegram / Discord 聊天接入)

把 agent 接到外部聊天平台:用户在 Telegram / Discord 私聊 bot,即与自己的 NimoOS agent 对话。代码在 `agent/channels/`,全部跑在 Python agent 进程内;**纯出站连接**(Telegram 长轮询 `getUpdates`、Discord Gateway WebSocket),家用 NAT 后无需公网入口、无需新增 JWT 豁免路由。设计稿 内部设计稿 `2026-07-02-nimoos-channels-design.md`(+ `2026-07-04-*-attachments-*`、`2026-07-05-*-interactive-*`)。

### 分层架构(`agent/channels/`)

| 文件 | 职责 |
|---|---|
| `model.py` | 平台无关消息模型:`InboundMessage` / `InboundAttachment` / `OutboundMessage`、`ChannelCapabilities`(长度上限/edit/buttons/typing/media)、抽象基类 `ChannelAdapter`、`split_text` 长文切分。平台怪癖只允许存在于 adapter 内 |
| `telegram.py` | `TelegramAdapter` — httpx 直连 Bot API,长轮询;4096 字上限,支持 typing/media/**buttons**(inline keyboard) |
| `discord.py` | `DiscordAdapter` — discord.py 走 Gateway WS,**DM-only**;2000 字上限,同样支持 buttons。discord.py 全部惰性 import(测试不必安装)。注意:bot 只能 DM 与其同服务器的用户 |
| `manager.py` | `ChannelManager` — adapter 生命周期;`reload()` 对比 DB 与运行中实例(停掉被删/禁用/改配置的,启动新启用的),启动时与每次实例管理写操作后调用 |
| `router.py` | `ChannelRouter` — 平台无关核心:外部身份 → NimoOS 用户(**deny-by-default 配对**)、chat → session 映射、命令处理(`/pair` `/whoami` `/new` `/stop`)、**每 chat 串行执行**(`asyncio.Lock` + `MAX_PENDING=3`)、确认按钮的展示与回调仲裁 |
| `driver.py` | `ChannelRunDriver` — 实时消费 RunSink:攒 `message_delta`,在每个工具调用边界与终态 `done` 把缓冲作为一条「阶段结论」推送(带 1s 最小发送间隔);遇 `access_request` / `confirmation_required` 先 flush 再交给 router 出按钮 |
| `collector.py` | `collect_final` — 只等终态、一次性回复的旧路径(M1;热路径已被 driver 取代) |
| `inbound.py` | 入站附件落盘:存到绑定的 `download_dir`(默认 `/DATA/Downloads/<channel_type>`)+ 经 `attachments.ingest.ingest_external` **symlink 注册为会话附件**;单文件/单消息 20MB、10 个上限,超限跳过并提示 |
| `credentials.py` | 凭据解析:调 Go 侧 localhost 内部端点 `GET /v1/ai/_internal/agent/provider-credentials?user_id&model`,带 `X-Internal-Token`(读 `/var/run/nimoos/ai_internal.token`)。云密钥只有 Go 层能解密,这是进程内消费者取密钥的唯一 sanctioned 途径(Go 侧 handler 在 `route/v2/channel_credentials.go`;裸模型名 = 本地 Ollama,`openvino:` 拒绝) |
| `store.py` | SQLite 存取(下表) |

### agent.db 中的表(`agent/db.py`)

| 表 | 用途 |
|---|---|
| `channel_instances` | bot 实例(channel_type / name / `config_json` 含 bot token —— **不回传前端**,API 只返回脱敏视图 / enabled / created_by) |
| `channel_bindings` | 外部账号 ↔ NimoOS 用户绑定(`UNIQUE(instance_id, external_user_id)`;per-binding `default_model`、`download_dir`、`revoked` 软删) |
| `channel_pairing_codes` | 配对码:仿 mcp_tokens —— **只存 sha256**、一次性、10 分钟 TTL |
| `channel_chats` | chat → agent session 映射(`/new` 换绑新 session) |

`sessions` 表新增 `source` 列(默认 `'web'`;channel 会话写 `'telegram'` / `'discord'`),channel 会话照常出现在 Web 会话列表里;`send_attachment` 工具按 `source != 'web'` 门控注册。

### 配对与安全

- **deny-by-default**:未绑定的外部账号只会收到「未配对」提示(每 external user 600s 至多一次),不触达 agent。
- 配对流:UI(`/#/ai/settings?section=channels`,`ChannelsSection.vue`)→ `POST /agent/channels/pairing-code` 生成 8 位数字码 → 用户在聊天里发 `/pair <code>`。爆破防护:每 external user 每小时 5 次失败后**静默**;stranger-keyed 限速字典有 4096 上限防 id 喷洒(`router._prune`)。
- 管理端点(经 Go 反代,JWT 保护):`/agent/channels/instances`(CRUD+enable)、`/agent/channels/pairable-instances`(任意用户可见的脱敏实例列表,供配对页;#46 补齐)、`/agent/channels/pairing-code`、`/agent/channels/bindings[/{bid}]`(撤销 / `PUT .../model` / `PUT .../download-dir`)。

### 附件收发(M2,#45)

- **入站**:adapter 下载(≤20MB)到临时文件 → `inbound.save_and_ingest` 移入 download_dir、注册为会话附件;纯附件消息注入一段占位提示文本让模型知道文件已落盘。
- **出站**:channel-only 工具 `send_attachment`(`agent/skills/send_attachment.py`)—— 经与 `read_document(path)` **同一条 fs 授权链**(`fs.ops._resolve_and_gate`:realpath + visible_resources 范围校验 + 黑名单)门控,但**不做交互式扩权**(越界直接拒);经 per-run 注入的 adapter `send_file` 回调**同步真发**,把真实成败返回给模型,绝不假报 "sent"。

### 交互式运行(#47)

- **进度推送**:`ChannelRunDriver` 取代 collect_final,长任务不再沉默到最后 —— 每个工具调用边界推一条阶段结论。
- **按钮权限确认**:agent 的 `access_request` / `confirmation_required` 事件渲染成 Telegram/Discord 内联按钮(✅ 允许 / ❌ 拒绝),回调经 `router.handle_confirm` 校验 chat/instance 归属后 resolve;**超时(默认 300s)/ 不支持按钮 / 发送失败一律降级为拒绝**,并把按钮消息原地编辑为结果行。`/stop` 会同时取消运行并确定性地 deny 该 session 的悬挂确认(避免挂到 ConfirmManager 的 24h 默认)。

启动接线在 `agent/main.py`:`_channels_startup()` 组装 `ChannelRouter`(start_run / cancel_run / credentials.resolve / confirm resolve)+ `ChannelManager.start_all()`,失败只告警不影响 agent 本体。

---

## Phoenix tracing(agent 可观测性,#41)

用 [Arize Phoenix](https://phoenix.arize.com/) 收 agent 运行的 OTLP trace。代码 `agent/phoenix_tracing.py`;设计稿 内部设计稿 `2026-06-30-agent-phoenix-tracing-design.md` / `*-tracing-productization-design.md`。

- **安装一次、门控随开**:启动时(若 OpenInference/OTel 依赖可 import)装 `OpenAIAgentsInstrumentor` + `GatedSpanExporter`;是否真正导出由进程内 `_enabled` 标志决定,来自 `user_settings` 表的全局 `tracing_enabled`(保留 user_id `__global__`),UI 开关经 `GET/PUT /agent/user-settings/tracing` 即时生效、**无需重启**。关闭时 exporter 直接丢弃、不碰网络 —— 停掉 Phoenix 不会刷 OTLP 重试日志。任何 setup 失败都被吞掉,agent 照常跑。
- **按 session 分组**:`build_trace_run_config` 给每次 run 一个 `RunConfig`(`workflow_name="nimoos-agent"`、`group_id=session_id`、metadata 带 user_id/model/agent_type);禁用时返回 `tracing_disabled=True`。
- **环境变量**:`NIMOOS_AGENT_TRACING=0/off` 硬性退出;`PHOENIX_OTLP_ENDPOINT`(默认 `http://127.0.0.1:6006/v1/traces`)、`PHOENIX_PROJECT`(默认 `nimoos-agent`)。
- **Phoenix 本体**以 NimoOS 应用形式跑:compose 清单 `agent/observability/phoenix_compose.yaml`(`arizephoenix/phoenix`,`:6006`,数据落 `/DATA/AppData/<AppID>`)。

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
bash scripts/install-ai.sh

# 启动 Python Agent(开发态)
bash scripts/start-ai.sh
```

参见仓库根 `scripts/`（随 NimoOS 安装脚本分发）:`install-ai.sh`、`start-ai.sh`、`deploy-agent.sh`。

---

## 与其他服务的关系

- **依赖 UserService**:从 `/var/run/nimoos/` 取公钥校验 JWT。
- **依赖 Gateway**:启动时通过 `POST /v1/gateway/routes` 注册 `/v1/ai` 和 `/doc/v1/ai`。
- **依赖 MessageBus**(规划/事件常量已声明):`AI:OllamaUnhealthy` / `AI:OllamaRecovered` 推送到前端。
- **被 UI 调用**:NimoOS-UI 的 AI 对话/设置面板。
- **出站连接外部聊天平台**(Channels):Telegram Bot API 长轮询、Discord Gateway WebSocket,均由 Python agent 主动外连,无入站暴露。
- **对外被 AI agent 调用**(MCP server):Claude Desktop 等经 `/v1/ai/mcp-rpc/` 调只读工具。

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

LLM-visible surface: an `<available-skills>` index (id + description of every enabled auto/slash skill, sanitized, 16 KiB cap) is injected into the system prompt on every run; the model loads a skill's instructions on demand via `read_skill_file(skill_id)`. Manual-trigger skills are hidden from the index and surface only via UI "Try in chat" injection (`X-Skill-Id` header). The former `list_skills` tool was removed as redundant.

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

Caveats:
- `manifest.json` `permissions` (`network` / `writable_paths`) is **declarative only** — nothing enforces it at runtime. Actual isolation comes from the sandbox (ro-bind `/skill` mount, offline-by-default netns, prlimit). Don't present it as a security boundary in UI copy.
- Editing a builtin skill requires bumping `BuiltinSeedVersion` in `service/skills_seed.go`, otherwise the idempotent seeder skips extraction on deploy.
- User skills must be created through the API/UI. Dropping files directly under `<root>/users/<uid>/` creates no DB row and never triggers a runtime-view rebuild, so the agent won't see them.
