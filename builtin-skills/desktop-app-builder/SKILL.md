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
  do NOT come back automatically while the same-named container keeps
  existing (tracked by container name). That deletion memory clears
  once the container has been gone past the ~45 s grace period, after
  which re-running it auto-returns it to the desktop; meanwhile it can
  be re-added from the desktop's add panel.
- **Never invent `nimoos.*` labels** beyond the contract table — they
  are not read by anything.
- Widget pages MUST be reachable without auth and MUST NOT set their
  own background — violating either shows "cannot connect" or a white
  panel in dark mode. Details are in widget-contract.md.
- Before modifying an existing app, inspect its current labels with
  `docker inspect <name> --format '{{json .Config.Labels}}'` — do not
  work from memory.
