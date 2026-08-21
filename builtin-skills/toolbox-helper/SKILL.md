## Toolbox helper

When the user asks to install a CLI tool or component (e.g. "帮我安装飞书 CLI",
"install gh", or pastes an `npx ... install` command):

### How to run
1. Call `expand_tools(["toolbox"])`, then `install_component(component_id)` with the
   catalog id (`lark-cli` for Feishu CLI, `gh` for GitHub CLI). A confirmation card is
   shown to the user; the install happens only after they approve.
2. Do NOT run `npm install -g`, `npx ... install`, `pip install` or similar in the
   sandbox for CLI tools: that writes into the container layer and is lost when the
   container is rebuilt. The toolbox persists on the host.
3. If the requested tool is not in the catalog, say so and offer: the user may drop a
   self-contained binary into `/var/lib/nimoos/ai/toolbox/bin/` (listed as "unmanaged"),
   or request it be added to the catalog.
4. For Feishu CLI specifically: after install, point the user to
   AI Settings → Feishu account to complete authorization (device-flow binding).

### Guardrails
- Never claim success before `install_component` returns.
- Never bypass the confirmation card.
