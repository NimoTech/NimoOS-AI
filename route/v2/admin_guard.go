package v2

import (
	"net/url"
	"path"
	"strings"

	"github.com/labstack/echo/v4"
)

// AdminScopedAgentPaths lists the agent endpoints that administer the whole
// box rather than one user's own data. Every entry is gated twice:
//
//   - route/v2.go registers it (and its `/*` subtree) with AdminOnly, ahead of
//     the general /agent/* wildcard;
//   - AdminPathGuard enforces the same check on the DECODED request path, so a
//     percent-encoded spelling that Echo routes to the wildcard instead is
//     still gated.
//
// Both layers read this one list. Adding an admin endpoint means adding it
// here — not writing another g.Any line.
//
// Why each one is admin-scoped:
//
//	channels/instances  bot configuration (tokens, enablement) for the box
//	channels/lark       enables/disables the box's outbound Feishu bot
//	shell-allowlist     governs unattended command execution
//	notes/settings      moves the system-wide notes root, migrating files
//	notes/dir-info      probes candidate notes folders for that settings UI
//	toolbox             installs global CLI components every sandbox shares
//	tasks               scheduled unattended runs, whose preauth document
//	                    hands out shell prefixes, fs_write roots and egress
//	                    domains — the same authority shell-allowlist governs
var AdminScopedAgentPaths = []string{
	"/agent/channels/instances",
	"/agent/channels/lark",
	"/agent/shell-allowlist",
	"/agent/notes/settings",
	"/agent/notes/dir-info",
	"/agent/toolbox",
	"/agent/tasks",
}

// adminCheckedContextKey marks a request whose admin role AdminPathGuard has
// already verified, so the per-route AdminOnly does not ask UserService a
// second time for the same request.
const adminCheckedContextKey = "nimoos.admin_checked"

// maxPathDecodePasses bounds the unescaping loop in NormalizeRequestPath.
// Repeated decoding is deliberate (see that function); the bound keeps a
// pathological input from spinning.
const maxPathDecodePasses = 4

// NormalizeRequestPath reduces a request path to the form a path check has to
// be made against: percent-decoded (repeatedly) and lexically cleaned.
//
// Echo routes on url.RawPath when it is set — i.e. on the ENCODED path — while
// the reverse proxy forwards url.Path, the DECODED one. That split is the whole
// bug this exists for: `/v1/ai/agent/ta%73ks` misses the static, admin-gated
// /agent/tasks route, falls through to the /agent/* wildcard ungated, and is
// then proxied to the agent as /agent/tasks, which serves it. Measured on 118
// before this fix: 200, with the run reaching the agent.
//
// Decoding runs more than once on purpose. One pass is what determines
// reachability today (net/url decodes once, the proxy re-escapes once, uvicorn
// decodes once), but "%2573" and friends are cheap to fold in, and
// over-decoding can only ever make MORE paths look admin-scoped — never fewer.
// For a deny check that is the safe direction.
//
// path.Clean also settles `%2f`: once decoded, `/agent/tasks%2fabc` is
// `/agent/tasks/abc`, which the prefix test below sees as inside the subtree.
func NormalizeRequestPath(raw string) string {
	p := raw
	for i := 0; i < maxPathDecodePasses; i++ {
		dec, err := url.PathUnescape(p)
		if err != nil || dec == p {
			// A path holding a stray '%' cannot be decoded further; judge what
			// we have rather than letting it through unjudged.
			break
		}
		p = dec
	}
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	// Collapses //, resolves . and .., and drops the trailing slash, so
	// /agent/tasks/, /agent/x/../tasks and //agent//tasks all land on
	// /agent/tasks.
	return path.Clean(p)
}

// IsAdminScopedPath reports whether `normalized` (already through
// NormalizeRequestPath) addresses one of AdminScopedAgentPaths, either exactly
// or somewhere in its subtree. `apiPrefix` is the mount point ("/v1/ai").
func IsAdminScopedPath(apiPrefix, normalized string) bool {
	for _, p := range AdminScopedAgentPaths {
		full := apiPrefix + p
		if normalized == full || strings.HasPrefix(normalized, full+"/") {
			return true
		}
	}
	return false
}

// AdminPathGuard enforces the admin role on every request whose DECODED path
// addresses an admin-scoped endpoint, whatever spelling reached the router.
//
// Registered with e.Use (after the JWT middleware, so an unauthenticated
// caller still gets 401 rather than 403) and independent of routing: it reads
// the request URL, never c.Path(), which is exactly why an encoded spelling
// cannot slip past it.
func AdminPathGuard(runtimePath, apiPrefix string) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			// EscapedPath() is the wire form (RawPath when set, else Path
			// re-escaped) — start from what the client actually sent.
			p := NormalizeRequestPath(c.Request().URL.EscapedPath())
			if !IsAdminScopedPath(apiPrefix, p) {
				return next(c)
			}
			allowed, err := enforceAdmin(c, runtimePath)
			if !allowed {
				return err
			}
			c.Set(adminCheckedContextKey, true)
			return next(c)
		}
	}
}
