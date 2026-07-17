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
| `nimoos.widget.w` | MAY | integer string | `2` | INITIAL width in grid cells; clamped to 2..4, invalid values become 2. Users can resize afterwards — the page must be responsive (see widget-contract.md) |
| `nimoos.widget.h` | MAY | integer string | `2` | INITIAL height in grid cells; clamped to 1..4, invalid values become 2. Same resize caveat as `w` |
| `nimoos.widget.minw` / `nimoos.widget.minh` | MAY | integer string | global `2` / `1` | min resizable width/height in grid cells, clamped into the global 2..4 / 1..4 range. Omit all four range labels = today's behavior (freely resizable within the global range) |
| `nimoos.widget.maxw` / `nimoos.widget.maxh` | MAY | integer string | global `4` / `4` | max resizable width/height in grid cells, clamped the same way; if min > max, min wins. **min == max locks the size — the desktop hides the resize handle**. The initial `w`/`h` is clamped into this range too |
| `nimoos.widget.resize` | MAY | `"false"` | resizable | sugar for `min=max=initial w/h` (locks the size; defaults to 2×2 when `w`/`h` are omitted). Explicit min/max labels take precedence |

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
- `docker stop` → the entry is removed on the next desktop poll
  (≤30 s): the backend positively reports the stopped state, so no
  grace period applies.
- `docker rm` → the entry is removed within ~1 minute (a 45 s absence
  grace period absorbs transient Docker enumeration blips first, so a
  brief blip inside that window leaves the desktop unchanged).
- Re-running a container with the same name (`docker start` /
  `docker run`) → back on the desktop within ≤30 s, position
  reassigned.
- Apps the user manually deleted from the desktop do NOT come back
  automatically while the container still exists (tracked by container
  name — keep names stable). That deletion memory clears as soon as
  the container is reported stopped (next poll) or has been absent
  past the grace period; re-running it after that auto-returns it to
  the desktop. Meanwhile, it can be re-added anytime from the
  desktop's add panel.

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
| a previously deleted app does not reappear | deletion memory persists while the container still exists → re-add from the desktop's add panel, or remove the container, wait >45 s, then run it again |
| my app vanished from the desktop | container stopped or removed (by design) → `docker start`/`docker run` brings it back within ≤30 s |
| label changes have no effect | labels cannot be hot-edited → `docker rm -f <name>`, then run/up again |
| widget problems ("cannot connect", white-on-white) | see `references/widget-contract.md` troubleshooting |
