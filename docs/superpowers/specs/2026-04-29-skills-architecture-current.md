# NimoOS-AI Agent Skills 现状架构（细化）

- **日期**：2026-04-29
- **范围**：把 `NimoOS-AI/agent/` 当前 skill 体系从请求路径到沙箱细节画清楚，作为后续"用户上传 skill"改造的基线。
- **作者**：haowen.lei

---

## 1. 请求路径（HTTP → AgentRunner）

```
POST /chat              main.py:120 (run_chat handler)
  X-User-Id, body{session_id, message, provider, model, ...}
        │
        ▼
  _assert_owns_session(session_id, user_id)        # main.py:54
        │
        ▼
  RunSink(session_id, _conn, _pubsub)              # SSE 推送 + 持久化
        │
        ▼
  _runner.run(session_id, user_id, message, sink,  # 全局单例 AgentRunner
              provider_key, provider_url, model,    # main.py:32, agent.py:157
              thinking, kind, ...)
        │
        ▼
  agent.py:178  per-session asyncio.Lock           # 同一 session 串行
        │
        ▼
  ContextVar 注入：session_id / sink / confirm_mgr / db / username / patterns
  （这是当前给静态 skill 传"会话上下文"的唯一通道，agent.py:181-197）
        │
        ▼
  Agent(name=..., tools=ALL_TOOLS, ...)            # ★ 这里 tools 是模块级常量
  Runner.run_streamed(agent, history+[user_msg])   # agent.py:240
        │
        ▼
  async for event in stream.stream_events():
      _convert_event(...) → sink.put({...})         # SSE: message/thinking/
                                                    # tool_call/tool_result/...
```

关键事实：

- **`AgentRunner` 是进程单例**（`main.py:32`），`tools=ALL_TOOLS` 在每次 `run()` 里重新构造 `Agent`，所以**已经具备按请求换工具集的能力**——只要把 `ALL_TOOLS` 换成"按 user_id 查出来的列表"。
- **没有 user 维度的工具过滤**——目前所有工具对所有人可见。

---

## 2. 工具装载链（import → ALL_TOOLS → SDK FunctionTool）

```
skills/__init__.py
   import skills.app_management        ─┐
   import skills.storage                │  每个模块顶层定义若干
   import skills.healthcheck            │  @function_tool async def foo(...)
   import skills.message_bus            │  并把它们收集到 module.ALL_TOOLS
   import skills.filesystem             │
   import skills.shell                 ─┘
   ALL_TOOLS = APP + STORAGE + HC + MB + FS + SHELL

@function_tool  (agents SDK 装饰器)
   ├─ 解析 Python 函数签名 → 自动生成 params_json_schema
   ├─ 解析 docstring        → tool description
   └─ 返回一个 FunctionTool 实例（agents/tool.py:281）
        FunctionTool {
          name: "run_command",
          description: "Run a bash command inside an isolated sandbox …",
          params_json_schema: {...},          # 给 LLM 看的
          on_invoke_tool: async (ctx, json_args) → str,
          is_enabled: bool | callable,        # ← 动态启停的 SDK 原生钩子
          needs_approval: bool | callable,    # ← 二次确认的 SDK 原生钩子
        }
```

两个**已经在 SDK 里、但目前没用上**的钩子：

- `is_enabled(ctx, agent) -> bool`：每次 run 时按 user/session 决定该工具是否暴露给模型。
- `needs_approval`：SDK 内置的"询问后再执行"机制（目前用自己的 `ConfirmManager` 实现确认流，不冲突）。

---

## 3. 单工具执行细节（以 `shell.run_command` 为例，最像未来用户脚本）

```
LLM 决定调用 run_command(command="ls /work", timeout_sec=30)
        │
        ▼
SDK 反序列化 JSON args → 调用被装饰的 async 函数  (skills/shell.py:120)
        │
        ▼
SESSION_ID_VAR.get()                           # 从 ContextVar 读会话
WORK_ROOT / session_id / "work"                # 会话私有目录
        │
        ▼
_build_argv(work, command)                     # skills/shell.py:43
   ┌─────────────────────────────────────────────────────────┐
   │ prlimit  --as=512MiB  --cpu=300  --nofile=1024          │
   │ bwrap                                                    │
   │   --ro-bind  /usr,/etc,/lib,/bin,/sbin                   │
   │   --proc /proc  --dev /dev  --tmpfs /tmp                 │
   │   --bind  <session_work_dir>  /work                      │
   │   --chdir /work  --unshare-all  --share-net              │
   │   --die-with-parent  --new-session                       │
   │   -- /bin/bash -lc "<command>"                           │
   └─────────────────────────────────────────────────────────┘
        │
        ▼
asyncio.create_subprocess_exec(...)
   stdout=PIPE  stderr=STDOUT  stdin=DEVNULL  start_new_session
        │
        ▼
wait_for(timeout)  → kill -KILL <pgid> on timeout
        │
        ▼
truncate to 16 KiB → return  "[exit N]\n<body>"
```

要点：

- 这是一个**已经做完的、可以给用户脚本复用的隔离层**：内存 / CPU / fd / 超时 / 输出大小 / 网络命名空间全有，文件系统是 ro 挂载只暴露 `/work`。
- **没有限制 `--share-net`**——如果用户脚本可上传，要考虑是否禁网或加黑名单（DNS 出网仍可被滥用作信标）。
- `WORK_ROOT/<session_id>/work` 会话私有，但目前**没有按 user_id 隔离的上层目录**——多用户场景下应改成 `WORK_ROOT/<user_id>/<session_id>/work`。

---

## 4. 数据库（agent.db, SQLite）

现状（推测自 `db.py` + `main.py` 查询）：

```
sessions          (id, user_id, title, created_at, updated_at)
messages          (id, session_id, role, content, created_at)   -- role='history' 整段快照
visible_resources (session_id, path, kind, added_at)            -- 文件系统授权
user_settings     (user_id, key, value)                         -- 例如 thinking_default
```

**完全没有 skill 相关表**。

---

## 5. 加上"用户上传 skill"后的最小改造图（方案 C：bash 脚本 + bwrap）

> 注：本节为方案 C 的速记，最终采用方案 B（声明式 markdown skill），见
> `2026-04-29-markdown-skills-design.md`。保留此节作对照参考。

### 5.1 新增表

```
user_skills (
  id            TEXT PK,
  owner_user_id TEXT,
  name          TEXT,
  description   TEXT,
  params_schema TEXT,
  script        TEXT,
  enabled       INTEGER,
  created_at    INTEGER,
  updated_at    INTEGER,
  UNIQUE(owner_user_id, name)
)
```

### 5.2 新增 HTTP

```
GET    /skills
POST   /skills      {name,desc,schema,script}
PATCH  /skills/{id} {... , enabled}
DELETE /skills/{id}
POST   /skills/{id}/dry-run {args}
```

### 5.3 装载链改造（agent.py）

```
原来：
  agent = Agent(tools=ALL_TOOLS, ...)

改为：
  dynamic_tools = _load_user_skills(conn, user_id)
  agent = Agent(tools=ALL_TOOLS + dynamic_tools, ...)
```

直接构造 `FunctionTool`，闭包里把 args 注入沙箱后复用 `shell.py:_run`。

### 5.4 安全边界

| 风险 | 处理 |
|---|---|
| 用户脚本读 `agent.db` / 调内部 `nimoos-cli` | 沙箱 PATH mask 掉相关 binary |
| 网络滥用 | 每个 skill 一个 `network_access` 标志，默认 false → 不传 `--share-net` |
| 同名冲突 | (owner, name) 唯一 |
| 配额 | 单脚本 ≤ 64 KiB；单用户最多 N 个 |

### 5.5 与 `ConfirmManager` 的关系

用户级 skill 默认走"必须确认"——`_make_invoker` 里先 `confirm_mgr.register + queue.put({type:"confirmation_required"})`。

---

## 一句话总结

现状是 **"导入期静态收集 → 全局单一 ALL_TOOLS → 每个请求构造同样的 Agent"**。沙箱、会话工作目录、确认流、SSE 推送都已经具备；关键扩展点是 **"按 user_id 在 run() 里动态拼装 tools 和 system prompt"**——SDK 本身完全支持。
