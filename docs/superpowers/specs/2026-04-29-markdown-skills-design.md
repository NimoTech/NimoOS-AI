# 用户自定义 Markdown Skills — 设计文档

- **日期**：2026-04-29
- **范围**：让用户上传/管理自己的 skill。skill 是一份 markdown 文档（可附带资源 / 代码片段），由代码渐进式披露注入系统提示词；模型按描述按需激活。形态对齐 Claude Code skill。
- **作者**：haowen.lei
- **关联**：现状基线见 `2026-04-29-skills-architecture-current.md`

---

## 1. 目标

1. 用户可以**上传、查看、编辑、删除**自己的 skill。
2. 每个 skill = 一份 `SKILL.md`（必须）+ 任意数量的资源文件（脚本、参考文档、片段，可选）。
3. **作用域：用户级**。skill 仅对其 owner 可见，其他用户的会话看不到。
4. **渐进式披露（progressive disclosure）**：
   - 系统提示词里**只**注入 `(name, description)` 的索引列表（约几十字节每条），不注入正文。
   - 模型判断需要时，主动调用 `use_skill(name)` 工具读取**正文**。
   - 正文里如果引用了 `scripts/foo.sh` / `references/api.md` 等资源，模型再调用 `read_skill_resource(name, path)` 取。
5. **添加即生效**：用户上传后，下一次发消息就能在系统提示词索引里看到，无需重启进程、无需重新登录。
6. **不执行用户代码**：skill 文档与资源都只是文本注入到模型上下文。如果 skill 教模型 "去跑这段 bash"，模型走的是已经存在的 `run_command`（bwrap 沙箱）通道，安全模型不变。

## 2. 非目标

- 不做"系统级 / 团队级 skill"（仅用户级）。
- 不做 skill 商店、版本管理、共享、依赖。
- 不在 v1 做 skill 内 `allowed-tools` 字段（先不限制 skill 能用哪些工具，全部继承当前会话权限；预留 frontmatter 字段以备未来扩展）。
- 不做 skill 的二进制资源（仅文本：`.md / .sh / .py / .json / .yaml / .txt` 等；图片等后续再说）。
- 不做服务器侧的 skill 执行/编辑器（仅 CRUD）。

## 3. 形态对齐 Claude Code

Claude Code 的 skill 形态：

```
<skill_root>/
  SKILL.md              # 必须，含 frontmatter
  scripts/...           # 可选，bash/python 等
  references/...        # 可选，附加文档
  其它任意子文件
```

`SKILL.md` 头部是 YAML frontmatter，主体是 markdown 指令：

```yaml
---
name: skill-name                    # ^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$
description: 何时使用 + 用来干什么。模型基于这段决定是否激活。
---

# 正文 markdown
正文里指挥模型怎么完成某类任务，可以引用同目录下的资源：
- 看 references/api.md 了解 API 字段
- 用 scripts/cleanup.sh 清理状态
```

本设计**完全沿用这个形态**，便于用户搬运/借鉴 Claude Code 生态的 skill。

## 4. 数据模型 / 存储布局

不入 DB，**直接落盘**——skill 本质就是文件。这样：
- 上传 = 写文件，立即生效；
- 删除 = 删目录；
- 编辑 = 覆盖文件；
- 不需要在 DB 里同步资源 blob，避免大对象。

```
$NIMOOS_AGENT_SKILLS_ROOT (默认 ~/.nimoos/agent/user_skills)
└── <user_id>/
    └── <skill_name>/
        ├── SKILL.md
        ├── scripts/
        │   └── ...
        ├── references/
        │   └── ...
        └── <任意相对路径>
```

**索引缓存**：每次 `AgentRunner.run()` 扫描 `<root>/<user_id>/*/SKILL.md`，解析 frontmatter，组装索引。开销：~几十次 `open` + 小 YAML 解析；可接受、不缓存。后续若用户 skill 数量大可加 mtime 缓存。

**扫描过滤**：必须显式跳过：

- 任何以 `.` 开头的目录（隐藏目录、`.tmp` 写入中目录）。
- 任何以 `.tmp` 结尾的目录（写入未完成或已崩溃残留）。
- 内部不存在 `SKILL.md` 的目录（坏数据，log warning 后跳过）。
- frontmatter 解析失败的 skill（log warning 后跳过，不中断本次 run）。

避免在 PUT/POST 写入中途被并发 `run()` 读到半成品。

**约束**：

| 项 | 上限 |
|---|---|
| 单用户 skill 数量 | 50 |
| 单 skill 总大小（所有文件相加） | 1 MiB |
| 单 skill 文件数量 | 100 |
| 单文件大小 | 256 KiB |
| `SKILL.md` 正文 | 32 KiB（注入到模型上下文要可控）|
| `description` 长度 | 256 字符（索引行要短）|

超限的请求 4xx 拒绝。

**`description` 字段的清洗（防 prompt 注入）**：description 会被原样拼进系统提示词，**必须在上传时清洗**：

- 禁止换行符（`\n` / `\r`）——只能是单行。
- 禁止 `<` 与 `>`——防止用户写 `</available-user-skills>\n[System Override] …` 闭合标签后注入伪指令。
- 禁止控制字符（U+0000–U+001F 除空格外、U+007F）。
- 上述任一出现 → 上传 4xx 拒绝（不要静默替换，避免用户以为生效）。

`SKILL.md` 正文同样会通过 `use_skill` 进入模型上下文，但**正文是 tool result 的位置**（不是系统提示词），且整段被工具调用语义包住，注入风险低于 description 直接拼系统提示词。正文不强清洗。

## 5. 渐进式披露（核心机制）

### 5.1 L1 — 索引注入

`agent.py:_compose_system_prompt` 在现有"visible_resources / agent.md"块之后追加：

```
<available-user-skills>
You have access to the following user-defined skills. Each skill is a
specialized procedure with its own instructions. To USE a skill, call
the `use_skill` tool with the skill's name to load its full instructions
into your context. Do this when the user's request matches the skill's
description.

- create-photo-album: Organize photos under a folder into a dated album with thumbnails.
- weekly-report: Generate a Markdown weekly report by aggregating commits and shell history.
- ...
</available-user-skills>
```

只列 `name + description`。无 skill 时整个 block 不出现。

### 5.2 L2 — 正文按需加载

新增内置工具 `use_skill(name: str) -> str`：

```
@function_tool
async def use_skill(name: str) -> str:
    """Load the full instructions for a user-defined skill by name.
    Call this when you've decided to apply one of the skills listed in
    <available-user-skills>. Returns the skill's SKILL.md body (without
    frontmatter). After reading, follow the instructions inside."""
    user_id = USER_ID_VAR.get()
    return _read_skill_body(user_id, name)
```

读取流程：
1. 校验 `name` 字符集（防路径穿越）。
2. 拼路径 `<root>/<user_id>/<name>/SKILL.md`。捕获**所有**可恢复异常并转成错误字符串返回，**不向 SDK 抛 Python 异常**：
   - `FileNotFoundError` → `"Error: skill '<name>' not found (may have been deleted)."`
   - `IsADirectoryError` / `NotADirectoryError` → `"Error: invalid skill layout."`
   - `PermissionError` → `"Error: permission denied reading skill."`
   - `UnicodeDecodeError` → `"Error: SKILL.md is not valid UTF-8."`
   - `yaml.YAMLError` → `"Error: SKILL.md frontmatter is not valid YAML."`
3. 用 `yaml.safe_load`（**禁用** `yaml.load`，防 Python 对象注入）解析 frontmatter，剥离，返回正文。
4. 正文 > 32 KiB 视为损坏（上传时已校验 ≤ 32 KiB），返回错误字符串提示用户重新上传。

### 5.3 L3 — 资源按需加载

新增内置工具 `read_skill_resource(skill: str, path: str) -> str`：

```
@function_tool
async def read_skill_resource(
    skill: str,
    path: str,
    offset: int = 0,
    limit: int = 65536,
) -> str:
    """Read a resource file bundled with a user skill. `skill` is the
    skill's name; `path` is a path relative to that skill's root
    (e.g. 'scripts/cleanup.sh', 'references/api.md'). Use this after
    use_skill() if the skill's body references a sibling file.
    Use `offset` (bytes) + `limit` (bytes, max 65536) to read large files
    in chunks."""
```

要点：

- `path` 必须是相对路径，正规化后（`os.path.realpath` + 前缀断言）仍在 `<root>/<user_id>/<skill>/` 内（拒绝 `..` / 绝对路径 / symlink 逃逸）。
- 仅文本 MIME；二进制返回错误字符串（不抛）。
- **不做静默截断**——文件 > `limit` 时按 `[offset, offset+limit)` 返回片段，并在末尾追加 `[truncated, total=<N> bytes; call again with offset=<offset+limit> to continue]`，让模型可以分块继续读，避免静默截断把 bash/JSON 切坏。
- 错误处理同 `use_skill`：`FileNotFoundError` / `PermissionError` / `IsADirectoryError` / `UnicodeDecodeError` 全部捕获返回友好字符串，**绝不向 SDK 抛异常**——尤其 run 进行中文件可能被另一个浏览器标签页 DELETE。

### 5.4 与现有 `agent.md` 机制的差异

- `agent.md`：是**文件系统授权资源**带的 README，写进系统提示词后**始终注入**（最大 32 KiB），全部前置可见。
- skill：**只索引前置可见**，正文与资源**仅在模型决定使用时**才进入上下文。

两者共存且不冲突。

## 6. 工具集组装（agent.py 改造）

```python
# 现状
agent = Agent(name=..., tools=ALL_TOOLS, ...)

# 新
agent = Agent(
    name=...,
    tools=ALL_TOOLS + [use_skill, read_skill_resource],
    ...
)
```

新增 ContextVar：

```python
# skills_md/context.py
USER_ID_VAR: ContextVar[str] = ContextVar("user_skill_user_id", default="")
```

`AgentRunner.run()` 在现有 ContextVar 注入处补一行：

```python
USER_ID_VAR.set(user_id)
```

## 7. 系统提示词注入（agent.py:_compose_system_prompt）

伪代码（追加在现有 `visible_resources` 处理之后）：

```python
def _compose_system_prompt(conn, session_id, user_id, base, ...):
    ...  # 现有 visible_resources 逻辑，最终得到 block

    skills = list_user_skills(user_id)   # [(name, description), ...]
    if skills:
        lines = ["", "<available-user-skills>",
                 "You have access to the following user-defined skills. ...",
                 ""]
        for name, desc in skills:
            lines.append(f"- {name}: {desc}")
        lines.append("</available-user-skills>")
        block += "\n".join(lines)

    return base + block
```

`list_user_skills(user_id)` 实现：扫描目录、解析 frontmatter、跳过坏的并 log 一行 warning。

## 8. HTTP API（main.py 新增）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/skills` | 列出当前 user 的所有 skill（name, description, file_count, size, updated_at）|
| `POST` | `/skills` | 创建一个 skill（body：完整 file map） |
| `GET` | `/skills/{name}` | 取 skill 详情（含 SKILL.md 全文 + 文件清单） |
| `GET` | `/skills/{name}/files/{path:path}` | 取某个资源文件全文 |
| `PUT` | `/skills/{name}` | 整体替换该 skill 的所有文件（原子：写到 `.tmp` 再 rename）|
| `DELETE` | `/skills/{name}` | 删除整个 skill 目录 |

**鉴权**：复用现有 `X-User-Id` Header。

**请求体格式（POST/PUT，application/json）**：

```json
{
  "files": {
    "SKILL.md": "---\nname: ...\ndescription: ...\n---\n...",
    "scripts/foo.sh": "#!/usr/bin/env bash\n...",
    "references/api.md": "..."
  }
}
```

为什么不用 multipart：UI 端从一个文件夹/编辑器组装 JSON 比 multipart 简单；服务端也省一层。文件少且文本，base64/原文都可（这里直接原文）。

**校验（统一在 POST/PUT 入口）**：
1. `files` 必须包含 `SKILL.md`。
2. 每个 key 是相对路径（无 `..`、无前导 `/`、normalize 后仍是相对，禁止以 `.` 开头的目录段）。
3. **所有文件内容强制 UTF-8**：服务端必须能 `bytes.decode("utf-8", errors="strict")` 通过；任何文件解码失败 → 4xx 拒绝并指明文件名。
4. 解析 `SKILL.md` frontmatter（**只用 `yaml.safe_load`**）：
   - 必须有 `name`、`description`；
   - `name` 与 URL 中的 `{name}` 一致；
   - `name` 匹配正则 `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$`；
   - `description` ≤ 256 字符，且通过第 4 节的字符清洗（无换行 / 无 `<` / `>` / 无控制字符）；
   - 正文 ≤ 32 KiB。
5. 计数与体积校验：
   - **POST**：`<root>/<user>/<name>` 不存在；当前 user 的 skill 数（按本节"扫描过滤"规则计数）+ 1 ≤ 50；新 skill 总大小 ≤ 1 MiB。
   - **PUT**：若 `<root>/<user>/<name>` 已存在，**不计入新增**——总数上限校验跳过；总大小校验用"用户其它 skill 总大小 + 新版本大小 ≤ 用户配额上限"（如果未来引入 per-user 总配额）。当前仅有 per-skill 上限，PUT 只需校验新版本 ≤ 1 MiB。
6. 资源文件 MIME 必须文本（按扩展名白名单：`.md / .txt / .sh / .py / .js / .ts / .json / .yaml / .yml / .toml / .ini / .conf / .sql / .html / .css`），其余拒绝。

**写入是原子的，且失败必须清理**：

- 写入到 `<root>/<user>/<name>.tmp-<uuid>/`（用 uuid 后缀避免并发 PUT 互踩）。
- 全部文件写完 `fsync` → 如果目标已存在先 `rename` 旧目录到 `<name>.old-<uuid>` → `rename` `.tmp-<uuid>` 到 `<name>` → 删除 `.old-<uuid>`。
- **任何阶段抛异常都必须在 `finally` 里 `shutil.rmtree(.tmp-<uuid>, ignore_errors=True)`**，避免崩溃残留垃圾占用配额、或被未来扫描器误读。
- 进程启动时跑一次 sweep：扫描 `<root>/*/` 下的 `.tmp-*` 与 `.old-*` 目录，超过一定时间（例如 1 小时）的强制清理。

## 9. 即时生效

- skill 列表与正文都在每次 `run()` 时**直接读盘**，无内存缓存、无 DB 同步。
- POST/PUT/DELETE 完成后 `fsync` 当前 skill 目录与父目录。
- 用户在前端点"保存" → 后端落盘 → 用户下次发消息时系统提示词的索引立即更新。

未来若性能成瓶颈：加一个 mtime → frontmatter 的进程内 LRU。先不加。

## 10. 安全模型

| 风险 | 处理 |
|---|---|
| 路径穿越 | 所有 `name` / `path` 均 normalize 后断言落在 `<root>/<user>/<name>/` 之内，拒绝 symlink 解析逃逸 |
| 系统提示词标签逃逸（description 注入） | description 强制单行 + 拒绝 `<` `>` + 拒绝控制字符；上传时校验失败直接 4xx |
| YAML 反序列化代码执行 | 仅使用 `yaml.safe_load`；禁止 `yaml.load`（在 lint 规则中 ban） |
| 编码混乱（GBK 等） | 上传时强制 UTF-8 strict 解码；运行时读盘亦 UTF-8；解码失败返回错误字符串而非异常 |
| 扫描读到半成品（`.tmp-*`）| 扫描显式跳过 `.` 前缀与 `.tmp-*` / `.old-*` 后缀；写入失败 finally 清理；启动时 sweep 旧残留 |
| 写入崩溃残留占配额 | 计数函数走"扫描过滤"规则——`.tmp-*` / `.old-*` 不计入用户 skill 数量与总大小 |
| 资源截断破坏脚本/JSON | 不做静默截断；`read_skill_resource` 提供 `offset+limit` 分块；超限返回结构化提示让模型分块继续 |
| run 中 skill 被并发删除 | `use_skill` / `read_skill_resource` 捕获 `FileNotFoundError` 等，返回错误字符串，不向 SDK 抛 |
| 注入攻击（来自 owner 自己） | skill 内容来自 owner 自己；与 owner 直接在 chat 里输入等价。模型仍受现有 ConfirmManager 守卫——所有 NAS 写操作要前端弹窗确认 |
| 大量上传塞爆磁盘 | 配额 + 单文件大小限制；上传时累加校验 |
| 私有信息泄露 | 索引仅对 owner 可见；其它用户 `list_user_skills(other_uid)` 返回空 |
| 模型滥用别人的 skill | 不可能——索引按 user_id 过滤 |
| 资源文件被 NAS 文件系统授权机制看到 | 不会——`/skills/...` 不会进 `visible_resources` 表，也不会被 filesystem 工具列出 |
| skill 教唆模型跑恶意 bash | `run_command` 走 bwrap 沙箱（ro 系统目录、tmpfs /tmp、独立 /work、prlimit 内存/CPU/超时），与现状一致 |

## 11. 实施步骤（建议拆分）

> 本节是设计预览的拆分思路，最终拆 plan 时以 `writing-plans` 为准。

1. **骨架与存储层**：新建 `agent/user_skills/` 包，含 `loader.py`（扫描 + frontmatter 解析）、`storage.py`（CRUD with 原子 rename + 配额校验）、`paths.py`（路径校验）。单测覆盖路径穿越、配额、坏 frontmatter。
2. **工具与系统提示词注入**：实现 `use_skill` / `read_skill_resource` 两个 FunctionTool；改 `agent.py:_compose_system_prompt` 与 `AgentRunner.run`；新增 `USER_ID_VAR`。集成测：跑一个最小 skill，断言 stream 里出现 `tool_call=use_skill` 后正文进入下一轮模型输入。
3. **HTTP API**：`main.py` 加 6 个 endpoint；e2e 测：POST 一个 skill → GET /skills 看到 → POST /chat 触发使用 → DELETE → GET /skills 为空。
4. **前端 UI（NimoOS-UI）**：AI 设置页加"我的 Skills"管理页（列表 / 新建 / 编辑 / 删除）；新建/编辑用一个简易 markdown + 文件树编辑器。本期可只支持单文件 SKILL.md，多文件资源放后续。

## 12. 开放问题

1. **是否在 v1 加 frontmatter 的 `allowed-tools` 字段？** 设计上保留字段位（解析后存进索引），但运行时暂不强制。Claude Code 内置该机制，未来要加便于平滑接入。
2. **二进制资源（图片、字体）支持？** 暂不做。模型读不到，也不影响主流程。
3. **是否暴露给 LLM 的工具清单里加 `list_skills`？** 不需要——索引已经在系统提示词里给了；多一个工具反而冗余。
4. **跨用户共享/团队 skill？** 明确不在 v1 范围。如果后期要做，按 `<root>/_shared/<skill>/` 与 ACL 表加层即可，不影响现有 per-user 路径。

---

## 一句话总结

把每个用户的 skill 当成一棵小目录树落盘；每次 run 扫描该用户目录，把 `(name, description)` 拼成索引贴进系统提示词；模型决定用时调 `use_skill` 读正文、调 `read_skill_resource` 读资源——**渐进式披露，添加即生效，不执行用户代码**，安全模型与现有 `run_command` 沙箱一致。

---

## 附录 A：评审反馈消化记录（2026-04-29）

来自 Gemini 的评审，6 条全部采纳，落点如下：

| # | 反馈 | 落入章节 |
|---|---|---|
| 1 | `.tmp` 目录脏读 + 残留 | §4 扫描过滤、§8 写入流程 finally、§10 安全表"扫描读到半成品 / 写入崩溃残留" |
| 2 | description prompt-injection（`</tag>` 逃逸）| §4 description 清洗、§10 安全表"系统提示词标签逃逸" |
| 3 | 资源截断破坏脚本 | §5.3 改为 `offset+limit` 分块 + 不静默截断、§10 安全表"资源截断破坏脚本/JSON" |
| 4 | PUT 配额边界陷阱 | §8 校验 5 区分 POST/PUT 路径 |
| 5 | UTF-8 强制 + `yaml.safe_load` | §8 校验 3/4、§10 安全表"YAML 反序列化代码执行 / 编码混乱" |
| 6 | `read_skill_resource` 异常态处理 | §5.2 `use_skill` 错误清单、§5.3 同款异常捕获、§10 安全表"run 中 skill 被并发删除" |
