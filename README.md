# NimoOS-AI

NimoOS 的 AI 服务 —— **LLM 推理网关** + **本地 Agent 运行时**。

> ### About / 关于本项目
>
> NimoOS is a fork of [CasaOS](https://github.com/IceWhaleTech/CasaOS)
> (Apache-2.0), originally developed by IceWhale Technology Co., Ltd.
> Building on that foundation, NimoOS adds an AI agent, RAG-based
> retrieval, a knowledge layer, and a built-in web terminal.
>
> NimoOS 基于 [CasaOS](https://github.com/IceWhaleTech/CasaOS)（Apache-2.0）
> fork 而来，原始项目由 IceWhale Technology Co., Ltd. 开发。在此基础上，
> NimoOS 重建了 AI Agent、RAG 检索、知识库与内置终端等能力。
>
> 归属详情见 [`NOTICE`](./NOTICE)。CasaOS 与 IceWhale 是 IceWhale Technology
> Co., Ltd. 的商标；NimoOS 是独立项目，与 IceWhale 无隶属关系。
>
> 本仓库是 NimoTech 原创，不含 CasaOS 衍生代码。

> ⚠️ Multi-user isolation is incomplete — Photos and Search are not yet
> per-user scoped. Read [SECURITY.md](./SECURITY.md#known-limitations)
> before deploying NimoOS for more than one person.
>
> ⚠️ 多用户隔离尚不完整（Photos 与搜索未按用户隔离）。若要给多人使用，
> 请先阅读 [SECURITY.md](./SECURITY.md#known-limitations)。

## 这是什么

绑定 localhost、由 NimoOS Gateway 转发，API 前缀 `/v1/ai`。由两个独立进程组成：

- **Go 服务** —— 推理路由、模型与供应商管理、对外 MCP server 端点
- **Python Agent 运行时** —— 工具调用、跨会话记忆、skills、聊天平台接入

## 主要能力

| 能力 | 说明 |
|---|---|
| 推理路由 | 本地（Ollama）与云端多供应商 / 多模型 |
| Agent 工具链 | 文件读写、shell、批量文件结构、文档阅读（含视觉页面） |
| 跨会话记忆 | 画像层 + 召回层 + 自动抽取 + 上下文压缩 |
| Skills | 渐进式披露 |
| MCP | 既是客户端（接外部 MCP server），也是 server（把 NimoOS 能力暴露给外部 AI 客户端） |
| 聊天平台接入 | Telegram · Discord |

## 安全模型

Agent 可以读文件、在服务器上执行 shell 命令。其约束是**行为层护栏 + 出站咽喉**，
**不是硬沙箱** —— 边界与适用范围见 [`SECURITY.md`](./SECURITY.md#ai-agent-security-model)。

## 构建

需要完整的 NimoOS monorepo checkout —— 所有 Go 服务通过 `replace` 指向本地的
`NimoOS-Common`，`go.mod` 里的版本号是装饰性的。

```bash
CGO_ENABLED=1 go build ./...   # go-systemd 需要 CGO
go test ./...                  # 注意：存在若干已知的既有失败用例
```

Python Agent 侧见 [`agent/`](./agent/)。

## 文档

架构、请求流转、事件与运行时细节见 [`OVERVIEW.md`](./OVERVIEW.md)。

## 许可

Apache-2.0，见 [`LICENSE`](./LICENSE)。
