# lark-cli fixture provenance

Recorded 2026-08-15 on 118 from **lark-cli v1.0.85**, run with a throwaway
`HOME` (never the real `~/.lark-cli`). Each file is the *verbatim* stream the
CLI produced — do not hand-edit; re-record instead.

| file | command | stream | exit |
|---|---|---|---|
| `config_show_not_configured.stderr.json` | `config show` | stderr | 3 |
| `auth_status_not_configured.stderr.json` | `auth status --json` | stderr | 3 |
| `whoami_not_configured.stderr.json` | `whoami` | stderr | 3 |
| `auth_login_nowait_not_configured.stderr.json` | `auth login --recommend --domain base,docs,im,drive --no-wait --json` | stderr | 3 |
| `config_init_new.stderr.txt` | `config init --new` (killed after 12s) | stderr | 124 (timeout) |
| `auth_logout_not_configured.stderr.txt` | `auth logout` | stderr | **0** |

Key facts these pin (see `lark/binding.py`'s module docstring):

1. **stdout is empty for every one of these.** All output — JSON envelopes, the
   ASCII QR block, the verification URL — goes to **stderr**.
2. "not configured" is exit **3** with
   `{"ok":false,"error":{"type":"config","subtype":"not_configured",...}}`.
   Every step of the flow returns this same envelope until `config init`
   completes, so the step-1 probe can key off the exit code alone.
3. `auth logout` exits **0** even with nothing to log out of ("No configuration
   found."), so its exit code carries no signal — treat it as best-effort.
4. `config init --new` has no `--json`; the URL must be scraped off the live
   stderr stream while the process is still blocked on `等待配置应用...`.

## NOT recorded — calibrate at Task 11

The success envelopes of `auth login --no-wait --json`, `auth login
--device-code --json` and a logged-in `whoami` need a completed app config plus
a real human authorizing in Feishu, which is impossible headlessly. Their field
*names* (`device_code`, `verification_url`, with aliases `verification_uri` /
`verification_uri_complete`) are confirmed from the CLI binary's own struct tags
and the bundled `lark-shared` skill doc, but the *envelope shape* is not. That
is why `binding._find_key()` searches the decoded JSON recursively instead of
pinning a path.
