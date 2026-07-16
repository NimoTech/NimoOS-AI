# desktop-app-builder 内置 skill 设计

日期:2026-07-16
状态:已与用户逐节确认,待实现

## 1. 背景与目标

`NimoOS-New-UI/docs/nimoos-app-ai-spec.md` 是一份写给"AI 编程助手"的桌面应用接入规范
(容器 label 契约 + widget iframe 页面契约)。它当初面向"在用户电脑上工作的外部 AI
助手"(如 Claude Code),而 NimoOS 自己的 agent 同样需要这份能力:用户对 agent 说
"帮我做一个桌面小组件",agent 应能独立完成编写、构建、上桌、自检的全流程。

目标:把该规范改造成 NimoOS-AI 的一个**内置 skill**(`desktop-app-builder`),
面向本机 agent 的第一视角重写操作性内容(方案 1,用户已选定),并利用系统现有的
skill 渐进式披露机制按需加载,给 agent 提供"AI coding 桌面应用/小组件"的能力。

同时确立**单一正本**:skill 成为 AI 版规范的唯一正本,docs 里的 AI 版原文件删除
(用户已确认"这个文件不应该放到这个地方");人类可读版 `nimoos-app-label-spec.md`
保留不动。

## 2. 非目标

- 不新增任何 Go/Python/UI 代码逻辑(除 seed 版本号与测试):渐进式披露、skill
  索引注入、`read_skill_file`、shell 确认卡片(`ConfirmCard.vue`)全部复用现有机制。
- 不做"无容器的小组件":机制上小组件页面必须由带 label 的 Docker 容器伺服,
  不存在独立小组件。
- 不在 bundle 内建 `templates/` 真实代码文件(方案 3 被否):模板以代码块形式
  留在契约文档中,紧邻解释文字,避免 bundle 内部出现第二份需同步的拷贝。

## 2.5 语言约定(2026-07-16 用户补充)

bundle 内**全部英文**(manifest description/title/examples、SKILL.md、两个
references),与现有 7 个内置 skill 一致;仅示例代码中面向最终用户的界面文案
(如 widget 演示页上的"下载任务")保留中文。agent 与用户对话仍用用户的语言。

## 3. Bundle 结构

```
NimoOS-AI/builtin-skills/desktop-app-builder/
├── manifest.json
├── SKILL.md                      # 入口(薄,约 2 KB):何时用 + 工作流 + 护栏
└── references/
    ├── app-contract.md           # 桌面应用:label 契约 + 项目骨架 + 构建运行 + 自检(约 4 KB)
    └── widget-contract.md        # 小组件:iframe 页面契约 + 设计套件 + 页面模板(约 4 KB)
```

渐进式披露(零新代码,均为现有机制):

- 第 0 层:`<available-skills>` 索引里的一行 description(每轮注入系统提示);
- 第 1 层:命中后 agent 调 `read_skill_file("desktop-app-builder")` 读 SKILL.md;
- 第 2 层:SKILL.md 指示按需读契约文件——**只做 app 读 app-contract.md 一份;
  做小组件两份都读**(单向依赖:widget 必须依附带 label 的容器,label 契约在
  app 文档里;反向不成立)。

## 4. manifest.json

所有取值均在 `service/skills_store.go::LoadManifest` 校验允许范围内:

| 字段 | 值 | 说明 |
|---|---|---|
| schema_version | 1 | |
| id / name | `desktop-app-builder` | 也是斜杠命令 `/desktop-app-builder` |
| title | `桌面应用开发` | |
| trigger | `auto` | 进索引,自动匹配 |
| description | 为 NimoOS Web 桌面开发应用与小组件:生成带 nimoos.* label 的 Docker 容器与符合契约的 widget 页面,自动出现在桌面。当用户想编写、修改或调试能显示在 NimoOS 桌面上的应用、图标或小组件时使用。 | 单行、无尖括号、≤256 字(校验硬性要求);决定自动触发命中率 |
| icon | `grid` | SkillIcon.vue 现有图标 |
| color | `blue` | 允许枚举内 |
| examples | "做一个能显示在 NimoOS 桌面上的应用"、"帮我写一个显示下载进度的桌面小组件" | |
| permissions | `{ "network": false, "writable_paths": [] }` | 与现有内置 skill 一致 |
| version / author | `0.1.0` / `Nimo` | |

## 5. SKILL.md 入口内容(四部分)

1. **背景一段话**:后端定时扫描 Docker 容器 label(前端 30 秒轮询),
   `nimoos.enable=true` 自动上桌;声明 `nimoos.widget.path` 的小组件自动落位;
   无需调 API、无需注册、无需重启服务。
2. **何时使用**:要"桌面上的应用/图标";要"桌面小组件";自建容器没上桌要排查。
3. **工作流(7 步)**:
   1. 判断形态:只要图标+网页 → 只读 app 契约;要小组件 → 两份都读
      (并说明小组件必须依附容器);
   2. 必读 `read_skill_file("desktop-app-builder", "references/app-contract.md")`;
   3. 涉及小组件再读 `references/widget-contract.md`;
   4. 与用户确认三件事(缺了别猜):应用名(=容器名,必须稳定)、宿主机端口
      (先查占用)、项目文件目录(默认建议 `/DATA/AppData/<应用名>/`);
   5. 用 `write_file`/`mkdir` 生成项目文件(骨架与模板在契约文件里);
   6. **问用户是否执行**:"文件已写好,要我现在构建并启动吗?"——同意后用
      `run_command` 跑 `docker build`/`docker run`(会弹现有确认卡片,属正常);
      用户不同意或 docker 不可用,把命令逐条解释给用户自己执行;
   7. 按契约文件末尾清单自检,最后告知用户"打开 `/app/` 等最多 30 秒"。
4. **护栏**:label 不能热改(改=重建容器);容器名要稳定(桌面按容器名记忆
   用户的删除操作);不要发明契约表以外的 `nimoos.*` label;改现有应用前先
   `docker inspect <名> --format '{{json .Config.Labels}}'` 看清现状。

## 6. 契约文件内容(来源映射与改写原则)

改写原则(方案 1):**硬性契约原样保留;操作性内容改写成 agent 在 NAS 本机的
第一视角**(`write_file`/`run_command`/`curl http://127.0.0.1/...`),消灭
`<NAS>` 占位符和"把命令给读者"的口吻。

### references/app-contract.md

| 内容 | 来源(原 spec) | 处理 |
|---|---|---|
| label 契约表(9 个 `nimoos.*` 键)+ MUST NOT | §1 | 原样保留 |
| 行为模型(30 秒上桌 / stop 变暗 / rm 消失 / 删除记忆按容器名) | §1 末 | 原样保留 |
| 项目骨架 + Dockerfile + docker run / compose 模板 | §3 | 模板保留;操作说明改 agent 视角 |
| 自检清单(app 部分) | §4 | `<NAS>` → `127.0.0.1`;标明哪些 agent 自己验证、哪条(浏览器看桌面)交给用户 |
| 故障速查(app 相关行) | §5 | 原样保留 |
| **新增**:端口占用检查(`docker ps` + `ss -ltn`) | — | 原 spec 无,agent 干活需要 |
| **新增**:最简可用 icon.svg 示例 | — | 原 spec 假设读者自备图标 |

### references/widget-contract.md

开头一句话声明依赖:"容器与 label 侧见 app-contract.md,本文件只讲 widget 页面"。

| 内容 | 来源(原 spec) | 处理 |
|---|---|---|
| iframe 页面契约(免鉴权 / 8 秒 / theme·lang·home 三参数 / sandbox 限制) | §2 | 原样保留 |
| `<head>` 设计套件引用模板 | §2 | 原样保留,加粗标注"逐字复制,`?v=2` 不可省" |
| MUST NOT(别自设背景 / 别 top 跳转 / 别依赖登录态) | §2 | 原样保留 |
| 设计套件组件类表 + `--nk-*` token 清单 | §2 | 原样保留 |
| 完整 widget 页面示例 | §3 | 原样保留 |
| 自检(免鉴权 curl、深浅主题可读)+ 故障表 widget 行 | §4/§5 | curl 改本机视角;widget 故障行并入 |

## 7. 代码侧改动

`NimoOS-AI` 仓库:

1. 新增 bundle 四文件(§3)。`//go:embed builtin-skills` 自动包含子目录,
   embed 代码零改动。
2. `service/skills_seed.go`:`BuiltinSeedVersion` `"7"` → `"8"`。
   **不升此号,已部署设备启动时跳过重新释放,新 skill 永远不落盘。**
3. `embed_builtin_skills_test.go`:版本断言改 `"8"`;新增
   `TestDesktopAppBuilderSkillEmbedded`(仿 file-reader 测试):manifest 可解析、
   `trigger=="auto"`、SKILL.md 与两个 references 文件均在 embed 中。

`NimoOS-New-UI` 仓库:

4. 删除 `docs/nimoos-app-ai-spec.md`(正本迁入 skill)。已确认(2026-07-16
   全仓库 grep):New-UI 中没有任何文件引用 ai-spec(引用是 ai-spec → label-spec
   单向),直接删除即可,无需改任何引用。

**不改**:agent Python 侧(`skills_registry.py` 通用扫描)、UI(Skills 页自动列出
新内置 skill;执行确认复用 `ConfirmCard.vue`)、Gateway/其他服务。

两仓库各自独立提交。

## 8. 验收标准

本地(实现阶段完成):

- `CGO_ENABLED=1 go build` 通过;embed/seed/store 相关 `go test` 全绿;
- manifest 通过 `LoadManifest` 全部校验(id 格式、trigger 枚举、description
  单行 ≤256 字、SKILL.md ≤50 KB)。

真机(用户部署后验证,部署命令 `nimo_os_docs/scripts/deploy.sh ai`):

- skills 根目录 `.version` 内容为 `8`,`builtin/desktop-app-builder/` 四文件齐全;
- UI Skills 设置页出现"桌面应用开发"卡片;
- 对 agent 说"帮我做一个显示时间的桌面小组件":agent 依次读 SKILL.md → 两份契约 →
  问应用名/端口/是否执行 → 弹确认卡片 → 构建运行 → 自检 → 小组件出现在桌面。

## 9. 风险与后续

- **规范演进**:今后改 label/widget 契约,只改 skill 内文件 + 升 seed 版本号;
  人类版 label-spec 如需同步为编辑者手动责任(两文件受众不同,允许详略差异)。
- **agent 抄模板手滑**:方案 1 接受此小概率风险;若实测频发,升级为方案 3
  (bundle 加 templates/ 真实文件)是纯增量改动。
- **记忆维护**:实现落地后更新 Claude 记忆 `desktop-app-label-recognition.md`
  ("AI 版文档须同步维护" → "AI 版正本已迁入 desktop-app-builder skill")。
