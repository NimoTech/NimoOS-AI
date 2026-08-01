# NimoOS-AI

NimoOS's AI service — provides the system with an **LLM inference gateway** + a **local agent runtime**. Current version `v0.1.0` (alpha).

Binds to localhost, forwarded by the Gateway; API prefix `/v1/ai`.

---

## Dual-Process Architecture

NimoOS-AI consists of two independent system processes:

```
                External request (forwarded by Gateway, /v1/ai/* only)
                            │
                            ▼
           ┌────────────────────────────────────┐
           │   nimoos-ai.service (Go, Echo)      │
           │   - LLM inference routing (local/cloud) │
           │   - Provider/Model/Session management │
           │   - Privacy policy + hard blacklist  │
           │   - Master Key encrypts API Keys     │
           │   - Reverse proxy /v1/ai/agent/*     │
           └────────────────────────────────────┘
                  │                    │
        ┌─────────▼──────────┐  ┌──────▼──────────────┐
        │  Local Ollama       │  │  nimoos-agent       │
        │  127.0.0.1:11434    │  │  127.0.0.1:8282     │
        │  (pre-installed)    │  │  (Python/FastAPI)   │
        └────────────────────┘  └─────────────────────┘
                                          │
                                  Cloud LLM (OpenAI/
                                  Anthropic/DeepSeek/
                                  Qwen, etc.)
```

- `nimoos-ai.service` — Go binary, main entry point, holds SQLite state.
- `nimoos-agent.service` — Python (FastAPI + openai-agents SDK), runs the tool-calling agent, main directory `agent/`.

The two communicate over local HTTP; the frontend reaches the Python agent via `/v1/ai/agent/*`, reverse-proxied through the Go service.

---

## API Routes (`/v1/ai`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/chat/completions` | OpenAI-compatible inference (Router decides local/cloud) |
| POST | `/messages` | Anthropic-compatible inference |
| GET / POST / PUT / DELETE | `/providers[/:id]` | Per-user cloud Provider CRUD |
| GET / PUT | `/policy` | Privacy policy (whether cloud is allowed, default backend, escalation confirmation) |
| GET / POST / DELETE | `/models[/:name]` | List/pull/delete local models (Ollama) |
| POST | `/models/pull` | Trigger `ollama pull` |
| GET | `/models/hf/search` | Search Hugging Face |
| GET | `/models/hf/files` | List model files |
| POST | `/models/hf/import` | Import from HF to local |
| GET / POST / DELETE / PATCH | `/sessions[/...]` | Chat session and message persistence |
| GET | `/services/status` | Ollama + Agent health status |
| GET | `/agent/health` | Agent health check |
| ANY | `/agent/*` | Reverse-proxied to the Python Agent (`:8282`) |
| GET | `/fs/mounts` | List mount points the Agent can access (picker scope) |
| GET / POST / DELETE | `/blacklist` | Hard blacklist (paths/patterns the AI must never see) |
| GET / POST / DELETE | `/mcp-tokens[/:id]` | Long-lived token management for the outward-facing MCP server (JWT-protected) |
| POST | `/mcp-rpc/` | **Outward-facing MCP server** data endpoint (JSON-RPC over Streamable-HTTP). **JWT-exempt** — validated instead via Bearer token on the Python side → user_id. See "Outward-Facing MCP Server" below |
| ANY | `/_internal/*` | Internal endpoints (chat/models/mcp runtime/**agent/provider-credentials**), **JWT-exempt + LocalhostOnly** (`route/middleware.go`). Of these, `GET /_internal/agent/provider-credentials` returns decrypted cloud secrets and additionally requires the `X-Internal-Token` shared secret (see "Channels") |

**Auth**: all routes except `/mcp-rpc/` enforce JWT validation, **with no localhost exemption**. This is deliberate — it prevents other local processes from reading another user's cloud API keys. After validation, `X-NimoOS-User-ID` / `X-NimoOS-User-Name` headers are injected. `/mcp-rpc/` is the sole exception: it's aimed at external AI agents and authenticates itself via a long-lived MCP token (see below).

Custom headers exposed via CORS: `X-NimoOS-Force-Cloud`, `X-User-Id`, `X-User-Name`, `X-Agent-Provider-Key/Url/Type`.

---

## Inference Routing Policy (Router)

`service/router.go` decides the backend based on user policy + request headers:

```
PrivacyPolicy {
  AllowRemote:      bool   # false => locked to local only
  DefaultBackend:   "local" | "cloud"
  EscalationPrompt: bool   # whether to prompt the user when escalating to cloud
}
```

| `AllowRemote` | `X-NimoOS-Force-Cloud` | `DefaultBackend` | Result |
|---|---|---|---|
| false | true | * | **Rejected** (`ErrRemoteNotAllowed`) |
| false | false | * | local (`ForceLocal=true`) |
| true | true | * | cloud |
| true | false | cloud | cloud |
| true | false | local | local |

---

## Automatic Provider Classification

`service/provider_classify.go` uses `(baseURL, protocol)` heuristics to classify into:
`deepseek` / `openai` / `anthropic` / `qwen` / `ollama` / `other`, used for UI display and a one-time `provider_type` backfill migration.

---

## Key Modules

| Package | Responsibility |
|---|---|
| `main.go` | Startup, binds a random port, writes `ai.url`, registers routes with the Gateway, starts Ollama health polling, systemd notify |
| `route/` | Echo routing, JWT middleware |
| `route/v2/` | Per-endpoint handlers (chat / providers / policy / models / sessions / agent / fs / blacklist) |
| `service/` | Business layer (DB migrations, routing decisions, provider/model/session management, Ollama health) |
| `pkg/config/` | Viper INI config loading |
| `pkg/crypto/` | Master Key (AES-GCM) used to encrypt Provider API Keys |
| `common/` | Constants (ports, API prefixes, event types) |
| `agent/` | Python Agent (FastAPI + openai-agents SDK), includes fs tools, skills, confirm mechanism |
| `build/sysroot/` | Install artifacts: systemd unit, sample config, setup/cleanup scripts |

---

## Data Storage

```
/etc/nimoos/ai.conf             # Config (INI)
/etc/nimoos/ai_master.key       # AES-GCM master key
/var/lib/nimoos/ai/
  ├── ai.db                     # SQLite: providers / privacy_policies /
  │                             #          chat_sessions / chat_messages /
  │                             #          models / hard_blacklist
  ├── models/                   # Model-related data
  └── agent/
      ├── agent.db              # Python Agent's own SQLite
      ├── snapshots/            # Agent operation snapshots
      └── venv/                 # Python virtual environment
/var/log/nimoos/                # Logs (zap → file; systemd also goes to journal)
/var/run/nimoos/ai.url          # Service-discovery address
/var/run/nimoos/ai_internal.token  # Randomly generated at startup (0600), shared secret for /_internal secret endpoints
```

### SQLite Schema

| Table | Purpose |
|---|---|
| `providers` | Per-user cloud Providers (name / base_url / api_key encrypted / protocol / provider_type / default_model) |
| `privacy_policies` | Per-user privacy policy |
| `chat_sessions` + `chat_messages` | Chat history |
| `models` | Local model inventory (sourced from ollama / huggingface) |
| `hard_blacklist` | Per-user hard blacklist (paths/patterns the Agent must never access) |

The `provider_type` column has an idempotent `ALTER TABLE` + a one-time backfill migration.

---

## Sample Config (`build/sysroot/etc/nimoos/ai.conf.sample`)

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

If `/etc/nimoos/ai.conf` doesn't exist at startup, the above sample is written out as the default.

---

## Python Agent (`agent/`)

A separate systemd unit `nimoos-agent.service`, runs on `:8282`, reverse-proxied by `nimoos-ai`.

| File | Purpose |
|---|---|
| `main.py` | FastAPI app: streaming responses, confirmations, snapshots |
| `agent.py` | `AgentRunner` — invokes the openai-agents SDK |
| `confirm.py` | Human-in-the-loop confirmation management before tool calls |
| `db.py` | Python-side SQLite (`agent.db`) |
| `fs/` | Filesystem tools (paths / ignore / staging / snapshots) |
| `skills/` | Built-in skills |
| `provider_adapters.py` | Adapts Provider config passed in from the frontend |
| `title_gen.py` | Automatic session title generation |
| `run_sink.py` | Run-event persistence and replay |
| `mcp_tokens.py` | SQLite storage/access for outward-facing MCP tokens (hashed storage, throttled `last_used_at` writes) |
| `mcp_server/` | Outward-facing MCP server (protocol adaptation + tool whitelist + path gating, see below) |
| `channels/` | Telegram / Discord chat platform integration (adapter/router/manager/driver, see "Channels" below) |
| `attachments/` | Session attachment layer (ingest / paths / extract / gc); inbound files from channels are registered via `ingest_external` |
| `phoenix_tracing.py` | Optional Phoenix (OTLP) tracing, gated by a toggle (see "Phoenix Tracing" below) |
| `observability/` | `phoenix_compose.yaml` — the NimoOS-app compose manifest for the Phoenix container |

Dependencies, see `agent/requirements.txt`: `fastapi`, `uvicorn`, `openai-agents`, `openai`, `httpx`, `pathspec`, `mcp` (MCP SDK), `discord.py` (Discord channel), `openinference-instrumentation-openai-agents` + `opentelemetry-sdk/-exporter-otlp-proto-http` (Phoenix tracing), `pypdf`/`python-docx`/`openpyxl`/`python-pptx` (attachment extraction).

The Agent receives the user's blacklist via an HTTP header (`X-Agent-User-Blacklist`, base64+JSON), which combines with the Go-side hard blacklist to form two layers of filtering.

---

## Outward-Facing MCP Server (for external AI agents)

Exposes this NAS's read-only knowledge capabilities as **MCP tools**, callable by external AI agents (Claude Desktop / Cursor, etc.) via the MCP protocol. This is the opposite direction from `mcp_client` (where NimoOS acts as an MCP client connecting out to others). Code lives in `agent/mcp_server/`.

**Endpoint and transport**: external address `http://<nas>/v1/ai/mcp-rpc/` (Go `nimoos-ai` reverse-proxies to the Python `:8282` `/mcp-rpc/`). Uses the official `mcp` SDK's **single Streamable-HTTP endpoint** (`stateless=True` + `json_response=True`); both the bare path and the trailing-slash form return 200 (`server.py` uses a raw-ASGI Route to avoid Starlette Mount's 307 redirect).

**Auth (Scheme H)**: long-lived tokens, `Authorization: Bearer nimoos_mcp_...`.
- Tokens are stored in the `mcp_tokens` table of `agent.db` (**hash only**; `last_used_at` writes throttled to 60s).
- Management endpoint `/v1/ai/mcp-tokens` (GET/POST/DELETE, **JWT-protected**, injects `X-NimoOS-User-ID`); the plaintext token is returned only once, at creation. UI at `/#/ai/settings?section=mcptokens`.
- The data endpoint `/mcp-rpc/` is **JWT-exempt**; the Python side validates the Bearer token → resolves `user_id`, passed to tools via `USER_ID_VAR`. Downstream calls to Search/Wiki/Photos then use `localhost + X-NimoOS-User-ID` (Scheme H was chosen over having Go issue an internal JWT, to avoid adding a new privileged endpoint + refresh mechanism).

**Tool whitelist (currently 9 tools, all read-only; write tools are never exposed)**: `nimoos_search`, `read_document` (`file_id` XOR `path`), `read_file_chunk`, `view_document_page` (renders a document page to PNG, returned as MCP **ImageContent** for the client's own vision model to look at — no server-side vision), `wiki_get_node`, `wiki_list_full_tree`, `wiki_recent_changes`, `search_photos`, `list_albums`.

**Path gating**: a headless MCP call has no chat session, so it can't use chat's `visible_resources` gating. `mcp_server/fs_gate.py` provides deny-only gating via `mcp_resolve_read_path()`: `realpath` guards against `..`/symlinks/sibling-prefix collisions (`/DATA-evil`), allows only `/DATA`, and carves out `/DATA/.system_data`; anything out of bounds raises `McpPathDenied`. All hard failures (gate denial / render failure / timeout / mutually exclusive params) are uniformly raised as `McpToolError` → mapped by `server._call` to `CallToolResult(isError=True)`.

**Modules**: `server.py` (ASGI/protocol adaptation + `_call` + `render_result`), `tools.py` (whitelist `TOOL_SPECS` + individual `_h_*` handlers + `ImageResult`/`McpToolError`), `fs_gate.py` (path gating). Design notes existed for this internally but have since been removed from this repo.

**Caveat**: the Photos data layer currently has no per-user filter → under multi-user setups, `search_photos`/`list_albums` cannot yet claim user isolation (no impact on a single-user NAS).

---

## Channels (Telegram / Discord chat integration)

Connects the agent to external chat platforms: a user DMs the bot on Telegram / Discord and is talking to their own NimoOS agent. Code lives in `agent/channels/`, all running inside the Python agent process; **outbound connections only** (Telegram long polling `getUpdates`, Discord Gateway WebSocket), so a home NAT setup needs no public ingress and no new JWT-exempt routes. Design notes for this existed internally but have since been removed from this repo.

### Layered Architecture (`agent/channels/`)

| File | Responsibility |
|---|---|
| `model.py` | Platform-agnostic message model: `InboundMessage` / `InboundAttachment` / `OutboundMessage`, `ChannelCapabilities` (length limit/edit/buttons/typing/media), abstract base class `ChannelAdapter`, `split_text` for splitting long text. Platform quirks are confined to the adapter |
| `telegram.py` | `TelegramAdapter` — talks to the Bot API directly via httpx, long polling; 4096-char limit, supports typing/media/**buttons** (inline keyboard) |
| `discord.py` | `DiscordAdapter` — uses discord.py over Gateway WS, **DM-only**; 2000-char limit, also supports buttons. discord.py is entirely lazy-imported (not required for tests). Note: the bot can only DM users who share a server with it |
| `manager.py` | `ChannelManager` — adapter lifecycle; `reload()` diffs the DB against running instances (stops deleted/disabled/reconfigured ones, starts newly enabled ones), called at startup and after every instance-management write |
| `router.py` | `ChannelRouter` — platform-agnostic core: external identity → NimoOS user (**deny-by-default pairing**), chat → session mapping, command handling (`/pair` `/whoami` `/new` `/stop`), **serialized per-chat execution** (`asyncio.Lock` + `MAX_PENDING=3`), and rendering/arbitrating confirmation buttons |
| `driver.py` | `ChannelRunDriver` — consumes the RunSink in real time: accumulates `message_delta`, and at each tool-call boundary plus the terminal `done` state, pushes the buffer as one "stage conclusion" (with a 1s minimum send interval); on `access_request` / `confirmation_required` it flushes first, then hands off to the router to render buttons |
| `collector.py` | `collect_final` — the older, one-shot-reply path that only waits for the terminal state (M1; superseded on the hot path by driver) |
| `inbound.py` | Inbound attachment persistence: saved to the bound `download_dir` (default `/DATA/Downloads/<channel_type>`) + registered as a session attachment via `attachments.ingest.ingest_external` **symlink registration**; capped at 20MB per file/message, 10 files max — over-limit files are skipped with a notice |
| `credentials.py` | Credential resolution: calls the Go-side localhost internal endpoint `GET /v1/ai/_internal/agent/provider-credentials?user_id&model`, with `X-Internal-Token` (read from `/var/run/nimoos/ai_internal.token`). Only the Go layer can decrypt cloud secrets, making this the sole sanctioned path for an in-process consumer to obtain them (the Go-side handler is in `route/v2/channel_credentials.go`; a bare model name means local Ollama, `openvino:` is rejected) |
| `store.py` | SQLite access (table list below) |

### Tables in agent.db (`agent/db.py`)

| Table | Purpose |
|---|---|
| `channel_instances` | Bot instances (channel_type / name / `config_json` containing the bot token — **never returned to the frontend**, the API only returns a redacted view / enabled / created_by) |
| `channel_bindings` | External account ↔ NimoOS user bindings (`UNIQUE(instance_id, external_user_id)`; per-binding `default_model`, `download_dir`, `revoked` soft-delete) |
| `channel_pairing_codes` | Pairing codes: modeled on mcp_tokens — **stores only sha256**, single-use, 10-minute TTL |
| `channel_chats` | chat → agent session mapping (`/new` rebinds to a new session) |

The `sessions` table gained a `source` column (default `'web'`; channel sessions write `'telegram'` / `'discord'`), so channel sessions show up normally in the Web session list; the `send_attachment` tool is only registered when gated on `source != 'web'`.

### Pairing and Security

- **deny-by-default**: an unbound external account only ever gets a "not paired" notice (at most once per 600s per external user), never reaching the agent.
- Pairing flow: UI (`/#/ai/settings?section=channels`, `ChannelsSection.vue`) → `POST /agent/channels/pairing-code` generates an 8-digit code → the user sends `/pair <code>` in chat. Brute-force protection: **silent** after 5 failures per external user per hour; the stranger-keyed rate-limit dict caps at 4096 entries to prevent ID-spray attacks (`router._prune`).
- Management endpoints (reverse-proxied through Go, JWT-protected): `/agent/channels/instances` (CRUD+enable), `/agent/channels/pairable-instances` (a redacted instance list visible to any user, for the pairing page; added in #46), `/agent/channels/pairing-code`, `/agent/channels/bindings[/{bid}]` (revoke / `PUT .../model` / `PUT .../download-dir`).

### Attachment Send/Receive (M2, #45)

- **Inbound**: the adapter downloads (≤20MB) to a temp file → `inbound.save_and_ingest` moves it into download_dir and registers it as a session attachment; attachment-only messages get an injected placeholder text so the model knows a file has landed.
- **Outbound**: the channel-only tool `send_attachment` (`agent/skills/send_attachment.py`) — gated through the **same fs authorization chain** as `read_document(path)` (`fs.ops._resolve_and_gate`: realpath + visible_resources scope check + blacklist), but with **no interactive privilege escalation** (out-of-scope is a flat denial); it sends synchronously via a per-run injected adapter `send_file` callback and reports the real outcome back to the model — it never falsely claims "sent".

### Interactive Runs (#47)

- **Progress push**: `ChannelRunDriver` replaces collect_final — long-running tasks no longer go silent until the end; a stage conclusion is pushed at every tool-call boundary.
- **Button-based permission confirmation**: the agent's `access_request` / `confirmation_required` events are rendered as Telegram/Discord inline buttons (✅ Allow / ❌ Deny); the callback is validated for chat/instance ownership via `router.handle_confirm` before being resolved; **timeout (default 300s) / unsupported buttons / send failure all degrade to a deny**, and the button message is edited in place into a result line. `/stop` both cancels the run and deterministically denies that session's pending confirmation (avoiding a hang on ConfirmManager's 24h default).

Startup wiring lives in `agent/main.py`: `_channels_startup()` assembles the `ChannelRouter` (start_run / cancel_run / credentials.resolve / confirm resolve) + `ChannelManager.start_all()`; failures only log a warning and don't affect the agent itself.

---

## Phoenix Tracing (agent observability, #41)

Uses [Arize Phoenix](https://phoenix.arize.com/) to collect OTLP traces of agent runs. Code in `agent/phoenix_tracing.py`; design notes for this existed internally but have since been removed from this repo.

- **Install once, gate toggles at runtime**: at startup (if the OpenInference/OTel deps are importable), `OpenAIAgentsInstrumentor` + `GatedSpanExporter` are installed; whether export actually happens is controlled by an in-process `_enabled` flag, sourced from the global `tracing_enabled` in the `user_settings` table (kept under user_id `__global__`); the UI toggle takes effect immediately via `GET/PUT /agent/user-settings/tracing`, **no restart needed**. When disabled, the exporter simply drops spans without touching the network — stopping Phoenix won't flood the logs with OTLP retries. Any setup failure is swallowed; the agent runs normally regardless.
- **Grouped by session**: `build_trace_run_config` gives each run a `RunConfig` (`workflow_name="nimoos-agent"`, `group_id=session_id`, metadata carrying user_id/model/agent_type); returns `tracing_disabled=True` when disabled.
- **Environment variables**: `NIMOOS_AGENT_TRACING=0/off` forces it off; `PHOENIX_OTLP_ENDPOINT` (default `http://127.0.0.1:6006/v1/traces`), `PHOENIX_PROJECT` (default `nimoos-agent`).
- **Phoenix itself** runs as a NimoOS app: compose manifest `agent/observability/phoenix_compose.yaml` (`arizephoenix/phoenix`, `:6006`, data lands in `/DATA/AppData/<AppID>`).

---

## Startup Order and Dependencies

systemd dependency chain (see `build/sysroot/usr/lib/systemd/system/nimoos-ai.service`):

```
nimoos-gateway.service ──┐
                         ├──▶ nimoos-ai.service ──▶ nimoos-agent.service
nimoos-message-bus.service ┘
```

`nimoos-ai` uses `Type=notify`; it's only considered started once `SdNotify(Ready)` fires.

External dependencies (pre-installed on the system):
- **Ollama** (`:11434`): polled by `OllamaChecker`; 3 consecutive failures emit an `AI:OllamaUnhealthy` event, and `AI:OllamaRecovered` fires on recovery.
- **GCC + CGO**: `go-sqlite3` requires `CGO_ENABLED=1`, same as the NimoOS core.
- **Python venv**: the Agent's venv is installed to `/var/lib/nimoos/ai/agent/venv/`, deployed by the setup scripts under `build/scripts/setup/service.d/ai/`.

---

## Build and Deploy

```bash
# Go service
cd NimoOS-AI && CGO_ENABLED=1 go build -o nimoos-ai .

# Multi-arch release (amd64 + arm64)
goreleaser release --snapshot --clean

# One-shot install script (system-level, includes Python venv + systemd)
bash scripts/install-ai.sh

# Start the Python Agent (dev mode)
bash scripts/start-ai.sh
```

See the repo root `scripts/` (distributed alongside the NimoOS install scripts): `install-ai.sh`, `start-ai.sh`, `deploy-agent.sh`.

---

## Relationship to Other Services

- **Depends on UserService**: fetches the public key from `/var/run/nimoos/` to validate JWTs.
- **Depends on Gateway**: registers `/v1/ai` and `/doc/v1/ai` via `POST /v1/gateway/routes` at startup.
- **Depends on MessageBus** (planned/event constants already declared): `AI:OllamaUnhealthy` / `AI:OllamaRecovered` pushed to the frontend.
- **Called by the UI**: NimoOS-UI's AI chat/settings panels.
- **Outbound connections to external chat platforms** (Channels): Telegram Bot API long polling, Discord Gateway WebSocket, both initiated outbound by the Python agent, with no inbound exposure.
- **Called by external AI agents** (MCP server): Claude Desktop, etc. invoke read-only tools via `/v1/ai/mcp-rpc/`.

---

## Design Notes

1. **API Key encryption**: the Master Key is persisted to `/etc/nimoos/ai_master.key` (0600); the `api_key` column in the Provider table is encrypted with it using AES-GCM + Base64.
2. **No localhost exemption**: unlike other services, the AI service enforces JWT even for local requests, because it proxies other people's cloud secrets.
3. **Layered hard blacklist**: the Go side manages the per-user blacklist (SQLite); at the start of each session, the Agent injects the current user's blacklist into the Python side via a header, where `pathspec` applies it on top as file-ignore rules.
4. **Routing decisions decoupled from the UI**: the UI only sends requests — `X-NimoOS-Force-Cloud` is triggered by the UI's "escalate to cloud" button — but whether it's actually allowed is decided by the backend Policy.

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
