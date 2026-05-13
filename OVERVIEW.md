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

**鉴权**:所有路由强制 JWT 校验,**无 localhost 豁免**。这是有意为之 — 防止本机其他进程读到其他用户的云 API Key。校验后注入 `X-NimoOS-User-ID` / `X-NimoOS-User-Name` Header。

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

依赖见 `requirements.txt`:`fastapi`、`uvicorn`、`openai-agents`、`openai`、`httpx`、`pathspec`。

Agent 通过 HTTP Header 接收用户黑名单(`X-Agent-User-Blacklist`,base64+JSON),与 Go 侧硬黑名单配合形成两层过滤。

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
