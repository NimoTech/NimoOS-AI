# desktop-app-builder Built-in Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a built-in NimoOS-AI skill (`desktop-app-builder`) that teaches the on-device agent to build Docker apps and widgets that auto-appear on the NimoOS web desktop, replacing `NimoOS-New-UI/docs/nimoos-app-ai-spec.md` as the single AI-facing source of truth.

**Architecture:** One skill bundle with progressive disclosure via existing mechanisms only: a one-line description in the `<available-skills>` index (L0) → SKILL.md entry (L1) → two on-demand contract files under `references/` (L2). App-only requests load one contract; widget requests load both (one-way dependency: widgets need the label contract). Zero new Go/Python/UI logic except the seed version bump and tests.

**Tech Stack:** Go 1.x (`go:embed`, testify), Markdown skill bundle. NimoOS-AI requires `CGO_ENABLED=1` (go-sqlite3).

**Spec:** `docs/superpowers/specs/2026-07-16-desktop-app-builder-skill-design.md` (in this repo).

## Global Constraints

- Two independent git repos: `/home/nimo/NimoTech/NimoOS-AI` (Tasks 1–4) and `/home/nimo/NimoTech/NimoOS-New-UI` (Task 5). Commit separately in each; never cross-commit.
- All Go commands in NimoOS-AI need `CGO_ENABLED=1`.
- Bundle language: **English everywhere** (manifest description/title/examples, SKILL.md, references). Exception: end-user-facing demo copy inside example code (e.g. widget demo page text) stays Chinese.
- `manifest.json` validation (`service/skills_store.go::LoadManifest`): `id` matches `^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$`; `trigger` ∈ auto|slash|manual; description single line, no `<`/`>`, ≤256 runes; SKILL.md must exist and be ≤50 KB.
- `icon` must exist in `NimoOS-UI/src/views/AI/Skills/SkillIcon.vue` (`grid` does); `color` ∈ blue|purple|pink|orange|green|teal|slate.
- Agent-side per-file read cap is 256 KB (`skills_registry.py`) — all bundle files are far below this.
- `BuiltinSeedVersion` MUST be bumped `"7"` → `"8"` or already-deployed devices will never extract the new bundle.
- Keep the existing 7 builtin bundles untouched.

---

### Task 1: Bundle skeleton — manifest.json + SKILL.md + embed test

**Files:**
- Modify: `/home/nimo/NimoTech/NimoOS-AI/embed_builtin_skills_test.go`
- Create: `/home/nimo/NimoTech/NimoOS-AI/builtin-skills/desktop-app-builder/manifest.json`
- Create: `/home/nimo/NimoTech/NimoOS-AI/builtin-skills/desktop-app-builder/SKILL.md`

**Interfaces:**
- Consumes: `builtinSkillsFS` (`embed.FS` in `embed_builtin_skills.go`, embeds `builtin-skills/`), `service.SkillManifest`.
- Produces: bundle dir `builtin-skills/desktop-app-builder/` with skill id `desktop-app-builder` (Tasks 2–4 add files under it and assert on this id).

- [ ] **Step 1: Write the failing embed test**

Append to `/home/nimo/NimoTech/NimoOS-AI/embed_builtin_skills_test.go`:

```go
func TestDesktopAppBuilderSkillEmbedded(t *testing.T) {
	b, err := builtinSkillsFS.ReadFile("builtin-skills/desktop-app-builder/manifest.json")
	require.NoError(t, err)
	var m service.SkillManifest
	require.NoError(t, json.Unmarshal(b, &m))
	require.Equal(t, "desktop-app-builder", m.ID)
	require.Equal(t, "auto", m.Trigger)

	// Description is injected into the system prompt: single line, no
	// angle brackets, ≤256 runes (mirrors validateSkillDescription).
	require.LessOrEqual(t, utf8.RuneCountInString(m.Description), 256)
	require.NotContains(t, m.Description, "\n")
	require.NotContains(t, m.Description, "<")
	require.NotContains(t, m.Description, ">")

	_, err = builtinSkillsFS.ReadFile("builtin-skills/desktop-app-builder/SKILL.md")
	require.NoError(t, err)
}
```

Add `"unicode/utf8"` to the import block of that file.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/nimo/NimoTech/NimoOS-AI && CGO_ENABLED=1 go test . -run TestDesktopAppBuilderSkillEmbedded -v`
Expected: FAIL — `ReadFile` returns `file does not exist`.

- [ ] **Step 3: Create manifest.json**

Write `/home/nimo/NimoTech/NimoOS-AI/builtin-skills/desktop-app-builder/manifest.json`:

```json
{
  "schema_version": 1,
  "id": "desktop-app-builder",
  "name": "desktop-app-builder",
  "title": "Desktop app builder",
  "description": "Build apps and widgets for the NimoOS web desktop: Docker containers with nimoos.* labels plus contract-compliant widget pages that auto-appear on the desktop. Use when the user wants to create, modify, or debug a desktop app, icon, or widget.",
  "color": "blue",
  "icon": "grid",
  "trigger": "auto",
  "examples": [
    "Make an app that shows up on my NimoOS desktop",
    "Build me a desktop widget that shows download progress"
  ],
  "permissions": { "network": false, "writable_paths": [] },
  "version": "0.1.0",
  "author": "Nimo"
}
```

- [ ] **Step 4: Create SKILL.md**

Write `/home/nimo/NimoTech/NimoOS-AI/builtin-skills/desktop-app-builder/SKILL.md`:

```markdown
## NimoOS desktop app builder

Build apps and widgets that appear automatically on the NimoOS web
desktop (`/app/`).

### Background (the whole mechanism in one paragraph)

The NimoOS backend periodically scans the labels of local Docker
containers (the desktop UI polls every 30 seconds). Any container
labeled `nimoos.enable=true` is picked up as a "desktop app": its icon
appears on the desktop automatically, and if the container also
declares `nimoos.widget.path`, the page it serves at that path is
embedded as a desktop widget (a glass-card iframe). No NimoOS API
calls, no registration, no service restarts — you only need (1)
correct labels and (2), for widgets, a page that follows the widget
contract.

### When to use

- The user wants an app / icon / tool that shows up on the desktop.
- The user wants a desktop widget (a small card showing status, data,
  or progress).
- The user's own container is not appearing on the desktop
  (troubleshooting).

### Workflow (progressive: read the contracts first, then build)

1. **Decide the shape.** Icon + web page only → read the app contract
   only. A desktop widget is wanted → read BOTH contracts. A widget
   must be served by a labeled container — there is no such thing as a
   container-less widget.
2. **Required reading:**
   `read_skill_file("desktop-app-builder", "references/app-contract.md")`
   — label contract, project skeleton, build & run, self-checks.
3. Widget involved? **Also read:**
   `read_skill_file("desktop-app-builder", "references/widget-contract.md")`.
4. **Confirm three things with the user (never guess):**
   - the app name (= container name; MUST stay stable — the desktop
     remembers user deletions by container name);
   - a host port (check for conflicts first — see the app contract);
   - where to put the project files (suggest
     `/DATA/AppData/<app-name>/`).
5. **Generate the project files** with `write_file` / `mkdir`. The
   skeleton and copy-paste templates are in the contract files.
6. **Ask before running:** "Files are ready — shall I build and start
   it now?" If the user agrees, run `docker build` / `docker run` via
   `run_command` (a confirmation card may pop up for the user — that
   is normal). If the user declines or docker is unavailable, hand
   them the exact commands with a one-line explanation each.
7. **Self-check** using the checklist at the end of the contract
   file(s), then tell the user: open `/app/` and wait up to 30
   seconds for the icon (and widget) to appear.

### Guardrails (common failure points)

- **Labels cannot be changed on a running container.** Changing labels
  means recreating it: `docker rm -f <name>`, then `docker run` /
  `docker compose up -d` again.
- **Keep the container name stable.** A renamed container is a new app
  to the desktop, and apps the user manually removed from the desktop
  do NOT come back automatically (by design — they can be re-added
  from the desktop's add panel).
- **Never invent `nimoos.*` labels** beyond the contract table — they
  are not read by anything.
- Widget pages MUST be reachable without auth and MUST NOT set their
  own background — violating either shows "cannot connect" or a white
  panel in dark mode. Details are in widget-contract.md.
- Before modifying an existing app, inspect its current labels with
  `docker inspect <name> --format '{{json .Config.Labels}}'` — do not
  work from memory.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/nimo/NimoTech/NimoOS-AI && CGO_ENABLED=1 go test . -run TestDesktopAppBuilderSkillEmbedded -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd /home/nimo/NimoTech/NimoOS-AI
git add builtin-skills/desktop-app-builder/manifest.json builtin-skills/desktop-app-builder/SKILL.md embed_builtin_skills_test.go
git commit -m "feat(skills): desktop-app-builder builtin skill — manifest + SKILL.md entry"
```

---

### Task 2: references/app-contract.md

**Files:**
- Modify: `/home/nimo/NimoTech/NimoOS-AI/embed_builtin_skills_test.go` (extend `TestDesktopAppBuilderSkillEmbedded`)
- Create: `/home/nimo/NimoTech/NimoOS-AI/builtin-skills/desktop-app-builder/references/app-contract.md`

**Interfaces:**
- Consumes: bundle dir from Task 1.
- Produces: `references/app-contract.md` — the path SKILL.md (Task 1) tells the agent to read.

- [ ] **Step 1: Extend the embed test (failing)**

In `TestDesktopAppBuilderSkillEmbedded`, add before the closing brace:

```go
	_, err = builtinSkillsFS.ReadFile("builtin-skills/desktop-app-builder/references/app-contract.md")
	require.NoError(t, err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/nimo/NimoTech/NimoOS-AI && CGO_ENABLED=1 go test . -run TestDesktopAppBuilderSkillEmbedded -v`
Expected: FAIL — `file does not exist` for `references/app-contract.md`.

- [ ] **Step 3: Create references/app-contract.md**

Write `/home/nimo/NimoTech/NimoOS-AI/builtin-skills/desktop-app-builder/references/app-contract.md`:

```markdown
## Desktop app contract — container labels, build & run

How discovery works: the NimoOS backend scans local Docker container
labels; the web desktop polls every 30 seconds. A container labeled
`nimoos.enable=true` appears on the desktop automatically. This file
covers the label contract, the project skeleton, building/running as
the on-device agent, and self-checks. Widget pages have their own
contract: `references/widget-contract.md`.

### Label contract (hard requirements)

| label | required | type/format | default | meaning |
|---|---|---|---|---|
| `nimoos.enable` | **MUST** | string `"true"` | — | recognition switch. Anything that is not the string true (e.g. True, 1) is NOT recognized |
| `nimoos.title` | **MUST** | any string | container name | display name on the desktop |
| `nimoos.icon` | SHOULD | URL, or path starting with `/` | none (letter tile) | icon. **A relative path means the app serves it itself** — the desktop builds `scheme://<NAS>:port<icon>`, so a relative icon **also requires `nimoos.port`** |
| `nimoos.scheme` | MAY | `http` / `https` | `http` | web UI protocol |
| `nimoos.port` | see meaning | numeric string | none | **host port** (the host side of `-p host:container`). Clicking the icon opens `scheme://<NAS>:port<index>`. **MUST be set when a widget or a relative icon is declared**, otherwise the widget shows "cannot connect" forever |
| `nimoos.index` | MAY | path | `/` | web UI entry path |
| `nimoos.widget.path` | required for widgets | path starting with `/` | none | **setting it = declaring a widget**. The desktop iframe loads `scheme://<NAS>:port<path>` |
| `nimoos.widget.w` | MAY | integer string | `2` | initial width in grid cells; clamped to 2..4, invalid values become 2 |
| `nimoos.widget.h` | MAY | integer string | `2` | initial height in grid cells; clamped to 1..4, invalid values become 2 |

MUST NOT:

- invent `nimoos.*` labels beyond this table (nothing reads them);
- put the container-internal port in `nimoos.port` — it must be the
  host-mapped port;
- expect label edits to affect a running container — labels are fixed
  at `docker run` / compose creation; changing them requires
  recreating the container (`docker rm -f` then run/up again).

### Behavior model (for answering user questions — no code needed)

- A new container lands on the desktop within ≤30 s: the icon takes
  the first free slot, and a declared widget auto-places too.
- `docker stop` → icon dims, widget shows "app not running";
  `docker start` restores both.
- `docker rm` → the desktop entry disappears; re-running a container
  with the same name puts it back automatically.
- Apps the user manually deleted from the desktop do NOT come back
  automatically (by design; tracked by container name — keep names
  stable). They can be re-added from the desktop's add panel.

### Project skeleton

```
<project-dir>/                  # suggest /DATA/AppData/<app-name>/
├── Dockerfile
└── html/
    ├── index.html              # main page (opens when the icon is clicked)
    ├── icon.svg                # desktop icon
    └── widget/index.html       # widget page — only if a widget is wanted
```

`Dockerfile` (copy verbatim):

```dockerfile
FROM nginx:alpine
COPY html /usr/share/nginx/html
```

Minimal usable `html/icon.svg` if the user has no icon (replace the
letter and color to taste):

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="4" y="4" width="56" height="56" rx="14" fill="#3b82f6"/><text x="32" y="42" font-size="30" text-anchor="middle" fill="#ffffff" font-family="sans-serif">A</text></svg>
```

`html/index.html` is ordinary — any page works; no NimoOS contract
applies to it. `html/widget/index.html` MUST follow
`references/widget-contract.md`.

### Picking a free host port (do this before writing files)

Check what is already taken, then pick a free port in the 18000–18999
range (uncommon, avoids well-known services):

```bash
docker ps --format '{{.Names}} {{.Ports}}'
ss -ltn
```

Run both via `run_command` and choose a port that appears in neither.

### Build and run (agent viewpoint)

You run on the NAS itself. After writing the project files with
`write_file` / `mkdir`, ask the user: "Files are ready — shall I build
and start it now?" On agreement, execute via `run_command` (a
confirmation card for the user is normal; if docker is unavailable or
denied, hand these exact commands to the user instead, one line of
explanation each):

```bash
docker build -t <app-name> <project-dir>
docker run -d --name <app-name> -p <port>:80 \
  --label nimoos.enable=true \
  --label "nimoos.title=<Display Name>" \
  --label nimoos.icon=/icon.svg \
  --label nimoos.port=<port> \
  <app-name>
```

Add these three lines before the image name when a widget is declared:

```bash
  --label nimoos.widget.path=/widget/ \
  --label nimoos.widget.w=2 \
  --label nimoos.widget.h=2 \
```

Equivalent compose file (labels sit under the **service**; they land
on the container as-is). Use this when the user prefers compose:

```yaml
services:
  <app-name>:
    image: <app-name>
    container_name: <app-name>
    ports:
      - "<port>:80"
    labels:
      nimoos.enable: "true"
      nimoos.title: "<Display Name>"
      nimoos.icon: "/icon.svg"
      nimoos.port: "<port>"
      nimoos.widget.path: "/widget/"   # widget only
      nimoos.widget.w: "2"             # widget only
      nimoos.widget.h: "2"             # widget only
```

### Self-check (run 1–3 yourself via run_command; hand 4 to the user)

1. `docker inspect <app-name> --format '{{json .Config.Labels}}'` —
   every `nimoos.*` key must match the table letter-for-letter
   (common typos: `nimoos.enabled`, `nimoos.widget.Path`).
2. `curl -s http://127.0.0.1/v2/app_management/web/appgrid` — the
   output must contain the app (search for the container name) with
   `"desktop":true`. If port 80 does not answer, the gateway
   auto-scans 80-89 then 8080-8089 at startup — try 8080 next. Found
   = the backend has recognized the app.
3. Widget only: check the widget URL per
   `references/widget-contract.md`.
4. Tell the user: open `/app/` in the browser and wait up to 30
   seconds — the icon (and widget) appear automatically.

### Troubleshooting

| symptom | cause → fix |
|---|---|
| nothing on the desktop after 30 s | label typo (self-check 1) / `enable` is not the string `"true"` / container not running (`docker ps`) |
| icon is a letter tile, not the real icon | `nimoos.icon` is a relative path but `nimoos.port` is missing, or the icon path 404s |
| a previously deleted app does not reappear | by design (the desktop remembers user deletions) → re-add from the desktop's add panel |
| label changes have no effect | labels cannot be hot-edited → `docker rm -f <name>`, then run/up again |
| widget problems ("cannot connect", white-on-white) | see `references/widget-contract.md` troubleshooting |
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/nimo/NimoTech/NimoOS-AI && CGO_ENABLED=1 go test . -run TestDesktopAppBuilderSkillEmbedded -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/NimoTech/NimoOS-AI
git add builtin-skills/desktop-app-builder/references/app-contract.md embed_builtin_skills_test.go
git commit -m "feat(skills): desktop-app-builder app contract (labels, build & run, self-checks)"
```

---

### Task 3: references/widget-contract.md

**Files:**
- Modify: `/home/nimo/NimoTech/NimoOS-AI/embed_builtin_skills_test.go` (extend `TestDesktopAppBuilderSkillEmbedded`)
- Create: `/home/nimo/NimoTech/NimoOS-AI/builtin-skills/desktop-app-builder/references/widget-contract.md`

**Interfaces:**
- Consumes: bundle dir from Task 1; label-side contract lives in Task 2's file (referenced, not duplicated).
- Produces: `references/widget-contract.md` — the second path SKILL.md tells the agent to read.

- [ ] **Step 1: Extend the embed test (failing)**

In `TestDesktopAppBuilderSkillEmbedded`, add before the closing brace:

```go
	_, err = builtinSkillsFS.ReadFile("builtin-skills/desktop-app-builder/references/widget-contract.md")
	require.NoError(t, err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/nimo/NimoTech/NimoOS-AI && CGO_ENABLED=1 go test . -run TestDesktopAppBuilderSkillEmbedded -v`
Expected: FAIL — `file does not exist` for `references/widget-contract.md`.

- [ ] **Step 3: Create references/widget-contract.md**

Write `/home/nimo/NimoTech/NimoOS-AI/builtin-skills/desktop-app-builder/references/widget-contract.md`:

```markdown
## Widget page contract — the iframe card

Container and label requirements live in
`references/app-contract.md`; this file only covers the page that
`nimoos.widget.path` points to. The desktop wraps that page in a
glass card (shell, title, and icon are drawn by the desktop — you are
responsible for the card CONTENT only). The iframe is sandboxed with
`sandbox="allow-scripts allow-same-origin allow-forms"` (**no**
allow-top-navigation).

### The page MUST

1. **Be reachable without authentication** — the desktop passes no
   token; a page that requires login shows "cannot connect" forever.
2. **Load within 8 seconds** — otherwise the desktop shows a
   placeholder.
3. **Handle three query parameters** (the desktop appends them):
   - `theme`: `dark` or `light` — set it on
     `<html data-theme="...">`;
   - `lang`: e.g. `zh_cn` — localize copy as needed;
   - `home`: the desktop's origin — used to load the design kit.
4. **Load the design kit with this exact template** (copy it into
   `<head>` verbatim — the `?v=2` version parameter is REQUIRED, the
   gateway cache depends on it):

```html
<script>
  const q = new URLSearchParams(location.search)
  document.documentElement.dataset.theme = q.get('theme') || 'dark'
  const l = document.createElement('link'); l.rel = 'stylesheet'
  l.href = (q.get('home') || '') + '/app/widget-kit.css?v=2'; document.head.appendChild(l)
</script>
```

### The page MUST NOT

- set its own background on `<html>`/`<body>` — the kit makes the
  background transparent so the desktop's glass card shows through.
  A custom background breaks dark mode: Chrome puts a white backing
  behind iframes whose `color-scheme` disagrees, and the kit's
  `color-scheme` declaration already handles this — do not override
  it;
- navigate with `top.location` or `<a target="_top">` (blocked by the
  sandbox). To open the app full-page use `window.open(url)` (opens a
  new tab);
- depend on cookies or localStorage from the NimoOS login session.

### Design kit classes (plain HTML + these classes = native look; use `var(--nk-*)` for every color, never hard-code)

| class | purpose | minimal example |
|---|---|---|
| `.nk-title` | small section title | `<p class="nk-title">下载任务</p>` |
| `.nk-stat` | big number (unit in nested `<small>`) | `<div class="nk-stat">3<small>个进行中</small></div>` |
| `.nk-label` | dimmed small label | `<span class="nk-label">今日</span>` |
| `.nk-list` + `.nk-row` (value in `.nk-value`) | key-value rows | `<ul class="nk-list"><li class="nk-row"><span>速度</span><span class="nk-value">2.1 MB/s</span></li></ul>` |
| `.nk-progress` (with `<i style="width:62%">`) | progress bar | `<div class="nk-progress"><i style="width:62%"></i></div>` |
| `.nk-badge` (optionally `.good`/`.bad`) | status dot | `<span class="nk-badge good">健康</span>` |
| `.nk-accent`/`.nk-good`/`.nk-bad` | text emphasis colors | `<span class="nk-value nk-accent">2.1 MB/s</span>` |

Available tokens (auto-switch between dark/light): `--nk-fg / --nk-muted / --nk-faint / --nk-accent / --nk-good / --nk-bad / --nk-divider / --nk-track / --nk-radius / --nk-font / --nk-num-font`.

Optional offline hardening: vendor a copy of the NAS's
`/app/widget-kit.css` into the app and fall back to it when the
templated load fails.

### Complete example page (copy whole, then change the business content)

`html/widget/index.html`:

```html
<!doctype html>
<html><head><meta charset="utf-8"><title>My Widget</title>
<script>
  const q = new URLSearchParams(location.search)
  document.documentElement.dataset.theme = q.get('theme') || 'dark'
  const l = document.createElement('link'); l.rel = 'stylesheet'
  l.href = (q.get('home') || '') + '/app/widget-kit.css?v=2'; document.head.appendChild(l)
</script>
</head><body>
  <p class="nk-title">演示任务</p>
  <div class="nk-stat">3<small>个进行中</small></div>
  <div class="nk-progress" style="margin:8px 0"><i style="width:62%"></i></div>
  <ul class="nk-list">
    <li class="nk-row"><span>速度</span><span class="nk-value nk-accent">2.1 MB/s</span></li>
    <li class="nk-row"><span>状态</span><span class="nk-badge good">健康</span></li>
  </ul>
</body></html>
```

### Widget self-check (run 1 yourself via run_command; hand 2 to the user)

1. `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<port><widget.path>` —
   must print `200` with no auth header of any kind.
2. Ask the user to check the widget in BOTH themes (desktop settings →
   theme toggle): text must be readable in each. White-on-white in
   dark mode almost always means the page set its own background or
   dropped `?v=2` from the kit URL — restore the verbatim template
   and delete any custom background styles.

### Troubleshooting

| symptom | cause → fix |
|---|---|
| icon appears but widget says "cannot connect" | `nimoos.port` missing / set to the container-internal port / widget page requires login / page took >8 s to load |
| white-on-white in dark mode | page set its own background or skipped the kit template → restore the verbatim `<head>` template, remove custom backgrounds |
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/nimo/NimoTech/NimoOS-AI && CGO_ENABLED=1 go test . -run TestDesktopAppBuilderSkillEmbedded -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/NimoTech/NimoOS-AI
git add builtin-skills/desktop-app-builder/references/widget-contract.md embed_builtin_skills_test.go
git commit -m "feat(skills): desktop-app-builder widget contract (iframe rules, design kit, template)"
```

---

### Task 4: Seed version bump + full-catalog validation + full test run

**Files:**
- Modify: `/home/nimo/NimoTech/NimoOS-AI/service/skills_seed.go:13` (`BuiltinSeedVersion`)
- Modify: `/home/nimo/NimoTech/NimoOS-AI/embed_builtin_skills_test.go` (version assertion + new round-trip test)

**Interfaces:**
- Consumes: `service.SeedBuiltinSkills(root string, src fs.FS) error`, `service.SkillsStore{Root}` with `ListBuiltin() ([]*SkillManifest, error)`, `service.BuiltinSeedVersion`.
- Produces: `BuiltinSeedVersion == "8"` — deployed devices re-extract the catalog on next start.

- [ ] **Step 1: Update the version assertion and add the round-trip test (failing)**

In `/home/nimo/NimoTech/NimoOS-AI/embed_builtin_skills_test.go`, change:

```go
func TestBuiltinSeedVersionBumped(t *testing.T) {
	require.Equal(t, "8", service.BuiltinSeedVersion)
}
```

Append (this seeds the real embedded FS to a temp dir and lists it through
`LoadManifest`, so an invalid manifest or missing SKILL.md in ANY bundle —
which `ListBuiltin` silently skips — fails loudly here):

```go
func TestAllBuiltinBundlesPassValidation(t *testing.T) {
	root := t.TempDir()
	require.NoError(t, service.SeedBuiltinSkills(root, builtinSkillsFS))
	store := &service.SkillsStore{Root: root}
	ms, err := store.ListBuiltin()
	require.NoError(t, err)
	ids := make([]string, 0, len(ms))
	for _, m := range ms {
		ids = append(ids, m.ID)
	}
	require.Contains(t, ids, "desktop-app-builder")
	// 7 pre-existing bundles + desktop-app-builder. A silently-skipped
	// (invalid) bundle would make this count drop.
	require.Len(t, ms, 8)
}
```

- [ ] **Step 2: Run tests to verify the expected failure**

Run: `cd /home/nimo/NimoTech/NimoOS-AI && CGO_ENABLED=1 go test . -run 'TestBuiltinSeedVersionBumped|TestAllBuiltinBundlesPassValidation' -v`
Expected: `TestBuiltinSeedVersionBumped` FAILS (`"7" != "8"`); `TestAllBuiltinBundlesPassValidation` PASSES (the bundle is already valid — this test guards regressions from here on).

- [ ] **Step 3: Bump the seed version**

In `/home/nimo/NimoTech/NimoOS-AI/service/skills_seed.go` change:

```go
const BuiltinSeedVersion = "8"
```

- [ ] **Step 4: Run the full NimoOS-AI test suite and build**

Run: `cd /home/nimo/NimoTech/NimoOS-AI && CGO_ENABLED=1 go build ./... && CGO_ENABLED=1 go test ./...`
Expected: build OK, all packages PASS (agent Python tests are not part of `go test`; nothing Python-side changed).

- [ ] **Step 5: Commit**

```bash
cd /home/nimo/NimoTech/NimoOS-AI
git add service/skills_seed.go embed_builtin_skills_test.go
git commit -m "feat(skills): bump builtin seed version to 8 for desktop-app-builder; validate full catalog"
```

---

### Task 5: Remove the superseded AI spec from NimoOS-New-UI

**Files:**
- Delete: `/home/nimo/NimoTech/NimoOS-New-UI/docs/nimoos-app-ai-spec.md`

**Interfaces:**
- Consumes: nothing (grep on 2026-07-16 confirmed no file in NimoOS-New-UI references `nimoos-app-ai-spec`; the only link is ai-spec → label-spec, one-way).
- Produces: the skill bundle (Tasks 1–3) is now the single AI-facing source of truth; `docs/nimoos-app-label-spec.md` (human version) stays.

- [ ] **Step 1: Re-verify nothing references the file**

Run: `grep -rn "nimoos-app-ai-spec" /home/nimo/NimoTech/NimoOS-New-UI/ --include="*.md" --include="*.ts" --include="*.vue" --include="*.js" | grep -v node_modules`
Expected: no output (exit code 1).

- [ ] **Step 2: Delete and commit (separate repo!)**

```bash
cd /home/nimo/NimoTech/NimoOS-New-UI
git rm docs/nimoos-app-ai-spec.md
git commit -m "docs: remove AI app spec — superseded by NimoOS-AI builtin skill desktop-app-builder"
```

---

### Task 6: On-device deployment note + memory update (no code)

**Files:**
- Modify: `/home/nimo/.claude/projects/-home-nimo-NimoTech/memory/desktop-app-label-recognition.md` (Claude memory, not a repo file)

- [ ] **Step 1: Update Claude memory**

In the memory file, replace the claim that the AI-facing doc lives in New-UI docs and "must be kept in sync" with: the AI-facing spec's single source of truth is now `NimoOS-AI/builtin-skills/desktop-app-builder/` (SKILL.md + references/); future contract changes are edited there **plus a `BuiltinSeedVersion` bump in `service/skills_seed.go`**; the human-readable `nimoos-app-label-spec.md` stays in New-UI docs and is synced manually as an editorial task.

- [ ] **Step 2: Tell the user how to deploy and verify on the device**

Deployment is the user's call (convention: deploy scripts only):

```bash
nimo_os_docs/scripts/deploy.sh ai
```

On-device verification (from the spec's acceptance criteria):
1. The skills root's `.version` file now contains `8` and `builtin/desktop-app-builder/` holds all four files.
2. The UI Skills settings page shows a "Desktop app builder" card.
3. Say to the agent: "帮我做一个显示时间的桌面小组件" — it should read SKILL.md, then both contracts, ask for name/port/run-consent (confirmation card appears), build, self-check, and the widget appears on the desktop.
