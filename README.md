# NimoOS-AI

The AI service for NimoOS — an **LLM inference gateway** plus a **local agent runtime**.

> ### About
>
> NimoOS is a fork of [CasaOS](https://github.com/IceWhaleTech/CasaOS)
> (Apache-2.0), originally developed by IceWhale Technology Co., Ltd.
> Building on that foundation, NimoOS adds an AI agent, RAG-based retrieval,
> a knowledge layer, and a built-in web terminal.
>
> See [`NOTICE`](./NOTICE) for attribution details. CasaOS and IceWhale are
> trademarks of IceWhale Technology Co., Ltd.; NimoOS is an independent
> project and is not affiliated with IceWhale.
>
> This repository is NimoTech's own work and contains no CasaOS-derived code.


> ⚠️ Multi-user isolation is incomplete — Photos and Search are not yet
> per-user scoped. Read [SECURITY.md](./SECURITY.md#known-limitations) before
> deploying NimoOS for more than one person.


## What this is
Binds localhost and is fronted by the NimoOS gateway under `/v1/ai`. It runs as
two separate processes:

- **Go service** — inference routing, model and provider management, the
  outward-facing MCP server endpoint
- **Python agent runtime** — tool calls, cross-session memory, skills, chat
  platform bridges

## Capabilities

| Capability | Notes |
|---|---|
| Inference routing | Local (Ollama) and cloud, multiple providers and models |
| Agent tooling | File read/write, shell, bulk file structure, document reading including visual pages |
| Cross-session memory | Profile layer, recall layer, automatic extraction, context compaction |
| Skills | Progressive disclosure |
| MCP | Both a client (connects to external servers) and a server (exposes NimoOS to external AI clients) |
| Chat bridges | Telegram, Discord |

## Security model

The agent can read files and run shell commands on your server. Its containment
is **behavioural guardrails plus an egress chokepoint — not a hard sandbox**.
Boundaries and intended scope: [`SECURITY.md`](./SECURITY.md#ai-agent-security-model).


## Building

NimoOS is a multi-repository project. Every Go service uses a `replace`
directive pointing at the local `NimoOS-Common` checkout, so a build needs the
full workspace — see
[NimoOS-Build](https://github.com/NimoTech/NimoOS-Build) for the layout and the
one-line clone helper.

`NimoOS-MessageBus` must be generated first; its generated API code is not
committed and other services' `go generate` consumes its OpenAPI spec.

```bash
CGO_ENABLED=1 go build ./...   # go-systemd needs CGO
go test ./...                  # note: some tests are known to fail already
```

Go services pin `go 1.21` and echo v4.12 — **do not run `go mod tidy`**.


The Python agent lives in [`agent/`](./agent/).


## Documentation

Architecture, request flow and runtime details: [`OVERVIEW.md`](./OVERVIEW.md).

## License

Apache-2.0 — see [`LICENSE`](./LICENSE).
