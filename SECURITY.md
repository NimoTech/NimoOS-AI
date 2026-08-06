# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue.

- Preferred: GitHub [private vulnerability reporting](https://github.com/NimoTech/NimoOS/security/advisories/new)
- Email: `nimonas@yeaher.com`

Please include: affected version, affected component, reproduction steps, and
the impact you observed.

**Our commitment:**

| | |
|---|---|
| Acknowledge your report | within 5 working days |
| Initial assessment | within 10 working days |
| Fix or documented mitigation | depends on severity; we will keep you updated |

We will credit reporters in the release notes unless you prefer otherwise.

## Supported versions

NimoOS is currently in the `v1.9.x-alpha` line. Only the latest release
receives security fixes. There is no long-term support branch yet.

## Known limitations

The Photos and Search subsystems (separate NimoOS services this agent's
skills can query on a user's behalf) do not yet enforce per-user data
isolation — see [NimoOS's Known
limitations](https://github.com/NimoTech/NimoOS/blob/main/SECURITY.md#known-limitations)
for details. Until that's closed, don't expose search/photo-retrieval skills
to untrusted users sharing an instance.

## Auth model

Unlike most other NimoOS services, `nimoos-ai` does **not** exempt localhost
callers from JWT validation — every `/v1/ai` route requires a valid user
token. This is deliberate: the service proxies each user's own cloud provider
API keys, so even a same-host caller must authenticate as that user before it
can use them. (A small set of `/_internal/*` paths are the exception, used
only for trusted service-to-service calls such as the wiki-summary worker.)

## AI agent security model

The built-in AI agent can read files and run shell commands on your server.
Its containment is built from **behavioural guardrails**, not a hard sandbox:

- **Command gating** — a policy layer inspects shell commands before execution
  and blocks destructive patterns.
- **Filesystem gating** — a deny-only gate restricts which paths the agent may
  read, with system-internal directories carved out.
- **Egress chokepoint** — the agent runs in a network namespace with no default
  route; all outbound traffic passes through a proxy that applies allow/deny
  rules and inspects uploads for sensitive content.
- **Audit log** — agent actions are recorded.

**What this means in practice:** these layers reliably stop accidents and
common prompt-injection paths. They should **not** be assumed to stop a
determined, targeted bypass by someone who can read this source code — which,
as an open-source project, is everyone.

Run the agent only with models and integrations you trust, and treat the
machine it runs on as one the agent has broad access to.
