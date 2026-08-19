package v2

import (
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func fakeUserService(t *testing.T, role string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			require.Equal(t, "/v1/users/current", r.URL.Path)
			require.Equal(t, "Bearer tok", r.Header.Get("Authorization"))
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"data":{"role":"` + role + `"}}`))
		}))
}

func gateCall(t *testing.T, runtimePath string) *httptest.ResponseRecorder {
	t.Helper()
	e := echo.New()
	next := func(c echo.Context) error { return c.String(http.StatusOK, "ok") }
	h := AdminOnly(runtimePath)(next)
	req := httptest.NewRequest(http.MethodGet, "/agent/channels/instances", nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec := httptest.NewRecorder()
	require.NoError(t, h(e.NewContext(req, rec)))
	return rec
}

func writeURLFile(t *testing.T, dir, url string) {
	t.Helper()
	require.NoError(t, os.WriteFile(
		filepath.Join(dir, "user-service.url"), []byte(url+"\n"), 0o644))
}

func TestAdminOnlyAllowsAdmin(t *testing.T) {
	us := fakeUserService(t, "admin")
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)
	rec := gateCall(t, dir)
	require.Equal(t, http.StatusOK, rec.Code)
}

func TestAdminOnlyRejectsNonAdmin(t *testing.T) {
	us := fakeUserService(t, "user")
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)
	rec := gateCall(t, dir)
	require.Equal(t, http.StatusForbidden, rec.Code)
}

func TestAdminOnlyUserServiceUnavailable(t *testing.T) {
	rec := gateCall(t, t.TempDir()) // no user-service.url file
	require.Equal(t, http.StatusServiceUnavailable, rec.Code)
}

func TestAdminGateRoutePrecedence(t *testing.T) {
	us := fakeUserService(t, "user") // non-admin: gate must reject with 403
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxied := 0
	proxy := func(c echo.Context) error { proxied++; return c.String(http.StatusOK, "proxied") }
	// Mirror route/v2.go registration order: gated routes first, wildcard last.
	e.Any("/agent/channels/instances", proxy, AdminOnly(dir))
	e.Any("/agent/channels/instances/*", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodPut, http.MethodDelete} {
		req := httptest.NewRequest(m, "/agent/channels/instances/abc123", nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusForbidden, rec.Code,
			"%s /agent/channels/instances/{id} must hit the gated route, not the wildcard", m)
	}

	// Sibling channel endpoints must fall through to the wildcard ungated.
	req := httptest.NewRequest(http.MethodPost, "/agent/channels/pairing-code", nil)
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, 1, proxied)
}

// TestShellAllowlistGateRoutePrecedence proves /agent/shell-allowlist[/*] is
// admin-gated (governs unattended shell-command execution) before falling
// through to the general /agent/* proxy wildcard. Mirrors
// TestAdminGateRoutePrecedence's registration order for channels/instances.
func TestShellAllowlistGateRoutePrecedence(t *testing.T) {
	us := fakeUserService(t, "user") // non-admin: gate must reject with 403
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxied := 0
	proxy := func(c echo.Context) error { proxied++; return c.String(http.StatusOK, "proxied") }
	// Mirror route/v2.go registration order: gated routes first, wildcard last.
	e.Any("/agent/shell-allowlist", proxy, AdminOnly(dir))
	e.Any("/agent/shell-allowlist/*", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodGet, http.MethodPost} {
		req := httptest.NewRequest(m, "/agent/shell-allowlist", nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusForbidden, rec.Code,
			"%s /agent/shell-allowlist must hit the gated route, not the wildcard", m)
	}

	req := httptest.NewRequest(http.MethodDelete, "/agent/shell-allowlist/abc123", nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusForbidden, rec.Code,
		"DELETE /agent/shell-allowlist/{id} must hit the gated route, not the wildcard")

	// Sibling agent endpoints must fall through to the wildcard ungated.
	req = httptest.NewRequest(http.MethodGet, "/agent/health", nil)
	rec = httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, 1, proxied)
}

// TestShellAllowlistGateAdminPassthrough proves an admin caller passes
// through the gate to reach the proxy on all three shell-allowlist verbs.
func TestShellAllowlistGateAdminPassthrough(t *testing.T) {
	us := fakeUserService(t, "admin")
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxy := func(c echo.Context) error { return c.String(http.StatusOK, "proxied") }
	e.Any("/agent/shell-allowlist", proxy, AdminOnly(dir))
	e.Any("/agent/shell-allowlist/*", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodGet, http.MethodPost} {
		req := httptest.NewRequest(m, "/agent/shell-allowlist", nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusOK, rec.Code, "%s admin must pass through", m)
	}

	req := httptest.NewRequest(http.MethodDelete, "/agent/shell-allowlist/abc123", nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code, "DELETE admin must pass through")
}

// TestNotesSettingsGateRoutePrecedence proves /agent/notes/settings is
// admin-gated (governs the system-wide notes root and file migration)
// before falling through to the general /agent/* proxy wildcard. Mirrors
// TestShellAllowlistGateRoutePrecedence's registration order.
func TestNotesSettingsGateRoutePrecedence(t *testing.T) {
	us := fakeUserService(t, "user") // non-admin: gate must reject with 403
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxied := 0
	proxy := func(c echo.Context) error { proxied++; return c.String(http.StatusOK, "proxied") }
	// Mirror route/v2.go registration order: gated routes first, wildcard last.
	e.Any("/agent/notes/settings", proxy, AdminOnly(dir))
	e.Any("/agent/notes/dir-info", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, path := range []string{"/agent/notes/settings", "/agent/notes/dir-info"} {
		for _, m := range []string{http.MethodGet, http.MethodPut} {
			req := httptest.NewRequest(m, path, nil)
			req.Header.Set("Authorization", "Bearer tok")
			rec := httptest.NewRecorder()
			e.ServeHTTP(rec, req)
			require.Equalf(t, http.StatusForbidden, rec.Code,
				"%s %s must hit the gated route, not the wildcard", m, path)
		}
	}

	// Sibling notes endpoints must fall through to the wildcard ungated.
	req := httptest.NewRequest(http.MethodGet, "/agent/notes", nil)
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, 1, proxied)
}

// TestNotesSettingsGateAdminPassthrough proves an admin caller passes
// through the gate to reach the proxy for notes settings.
func TestNotesSettingsGateAdminPassthrough(t *testing.T) {
	us := fakeUserService(t, "admin")
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxy := func(c echo.Context) error { return c.String(http.StatusOK, "proxied") }
	e.Any("/agent/notes/settings", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodGet, http.MethodPut} {
		req := httptest.NewRequest(m, "/agent/notes/settings", nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusOK, rec.Code, "%s admin must pass through", m)
	}
}

// TestToolboxGateRoutePrecedence proves /agent/toolbox[/*] is admin-gated
// (installs/removes global CLI components shared by every sandbox session)
// before falling through to the general /agent/* proxy wildcard. Mirrors
// TestShellAllowlistGateRoutePrecedence's registration order.
func TestToolboxGateRoutePrecedence(t *testing.T) {
// TestWebSettingsRequiresAdmin proves /agent/web-settings is admin-gated
// (governs the box-wide search provider and API key, which every user's
// web_search then spends) before falling through to the general /agent/*
// proxy wildcard. A non-admin request must be refused and must never reach
// the agent proxy. Mirrors TestNotesSettingsGateRoutePrecedence's
// registration order.
func TestWebSettingsRequiresAdmin(t *testing.T) {
	us := fakeUserService(t, "user") // non-admin: gate must reject with 403
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxied := 0
	proxy := func(c echo.Context) error { proxied++; return c.String(http.StatusOK, "proxied") }
	// Mirror route/v2.go registration order: gated routes first, wildcard last.
	e.Any("/agent/toolbox", proxy, AdminOnly(dir))
	e.Any("/agent/toolbox/*", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodGet, http.MethodPost} {
		req := httptest.NewRequest(m, "/agent/toolbox", nil)
	// Mirror route/v2.go registration order: gated route first, wildcard last.
	e.Any("/agent/web-settings", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodGet, http.MethodPut} {
		req := httptest.NewRequest(m, "/agent/web-settings", nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusForbidden, rec.Code,
			"%s /agent/toolbox must hit the gated route, not the wildcard", m)
	}

	req := httptest.NewRequest(http.MethodPost, "/agent/toolbox/install", nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusForbidden, rec.Code,
		"POST /agent/toolbox/install must hit the gated route, not the wildcard")

	// Sibling agent endpoints (e.g. per-user lark binding) must fall through
	// to the wildcard ungated.
	req = httptest.NewRequest(http.MethodGet, "/agent/lark/binding", nil)
	rec = httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, 1, proxied)
}

// TestToolboxGateAdminPassthrough proves an admin caller passes through the
// gate to reach the proxy on both toolbox verbs.
func TestToolboxGateAdminPassthrough(t *testing.T) {
	us := fakeUserService(t, "admin")
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxy := func(c echo.Context) error { return c.String(http.StatusOK, "proxied") }
	e.Any("/agent/toolbox", proxy, AdminOnly(dir))
	e.Any("/agent/toolbox/*", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodGet, http.MethodPost} {
		req := httptest.NewRequest(m, "/agent/toolbox", nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusOK, rec.Code, "%s admin must pass through", m)
	}

	req := httptest.NewRequest(http.MethodPost, "/agent/toolbox/install", nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code, "POST /agent/toolbox/install admin must pass through")
}

// TestTasksGateRoutePrecedence proves /agent/tasks[/*] is admin-gated before
// falling through to the general /agent/* proxy wildcard. Scheduled tasks are
// unattended runs whose preauth document hands out shell prefixes, fs_write
// roots and egress domains — the same authority shell-allowlist governs.
func TestTasksGateRoutePrecedence(t *testing.T) {
	us := fakeUserService(t, "user") // non-admin: gate must reject with 403
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxied := 0
	proxy := func(c echo.Context) error { proxied++; return c.String(http.StatusOK, "proxied") }
	// Mirror route/v2.go registration order: gated routes first, wildcard last.
	e.Any("/agent/tasks", proxy, AdminOnly(dir))
	e.Any("/agent/tasks/*", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodGet, http.MethodPost} {
		req := httptest.NewRequest(m, "/agent/tasks", nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusForbidden, rec.Code,
			"%s /agent/tasks must hit the gated route, not the wildcard", m)
	}

	// Every nested task endpoint, including the two-segment ones.
	nested := []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/agent/tasks/notify-targets"},
		{http.MethodGet, "/agent/tasks/abc123"},
		{http.MethodPut, "/agent/tasks/abc123"},
		{http.MethodDelete, "/agent/tasks/abc123"},
		{http.MethodPost, "/agent/tasks/abc123/run"},
		{http.MethodGet, "/agent/tasks/abc123/runs"},
		{http.MethodPost, "/agent/tasks/abc123/preauth/from-denied"},
	}
	for _, c := range nested {
		req := httptest.NewRequest(c.method, c.path, nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusForbidden, rec.Code,
			"%s %s must hit the gated route, not the wildcard", c.method, c.path)
	}

	// Per-user sibling endpoints must stay ungated.
	for _, path := range []string{"/agent/lark/binding", "/agent/sessions"} {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusOK, rec.Code, "%s must stay ungated", path)
	}
	require.Equal(t, 2, proxied)
}

// TestTasksGateAdminPassthrough proves an admin caller passes through the gate
// to reach the proxy on the collection and on a nested task endpoint.
func TestTasksGateAdminPassthrough(t *testing.T) {
	us := fakeUserService(t, "admin")
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	proxy := func(c echo.Context) error { return c.String(http.StatusOK, "proxied") }
	e.Any("/agent/tasks", proxy, AdminOnly(dir))
	e.Any("/agent/tasks/*", proxy, AdminOnly(dir))
	e.Any("/agent/*", proxy)

	for _, m := range []string{http.MethodGet, http.MethodPost} {
		req := httptest.NewRequest(m, "/agent/tasks", nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusOK, rec.Code, "%s admin must pass through", m)
	}

	for _, c := range []struct{ method, path string }{
		{http.MethodDelete, "/agent/tasks/abc123"},
		{http.MethodPost, "/agent/tasks/abc123/run"},
	} {
		req := httptest.NewRequest(c.method, c.path, nil)
		req.Header.Set("Authorization", "Bearer tok")
		rec := httptest.NewRecorder()
		e.ServeHTTP(rec, req)
		require.Equalf(t, http.StatusOK, rec.Code,
			"%s %s admin must pass through", c.method, c.path)
	}
}
			"%s /agent/web-settings must hit the gated route, not the wildcard", m)
	}
	require.Equal(t, 0, proxied, "non-admin request must never reach the agent proxy")

	// Sibling agent endpoints must fall through to the wildcard ungated.
	req := httptest.NewRequest(http.MethodGet, "/agent/health", nil)
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, 1, proxied)
}

// TestAdminGatedAgentPathsPrecedeWildcard asserts, against the source of
// route/v2.go itself, that every admin-gated agent path is registered
// before the general "/agent/*" proxy wildcard. This is the one thing the
// httptest-based precedence tests above (TestAdminGateRoutePrecedence,
// TestShellAllowlistGateRoutePrecedence, TestNotesSettingsGateRoutePrecedence,
// TestWebSettingsRequiresAdmin) cannot catch: they all hand-build their own
// echo.New() and re-register the routes to match what v2.go is *supposed*
// to do, so none of them would fail if the real registration in v2.go were
// deleted, reordered after the wildcard, or typo'd. Echo's router falls back
// to whichever handler is registered for a path, and a wildcard registered
// ahead of a specific path shadows it — so a gated pair that ends up after
// "/agent/*" in the source never matches, and the endpoint becomes silently
// reachable by any authenticated user, not just admins.
func TestAdminGatedAgentPathsPrecedeWildcard(t *testing.T) {
	// route/v2.go lives one directory up from this package (route/v2/).
	const v2GoPath = "../v2.go"
	raw, err := os.ReadFile(v2GoPath)
	require.NoErrorf(t, err, "read %s (adjust the relative path if the test binary's working directory changes)", v2GoPath)
	src := string(raw)

	wildcardRe := regexp.MustCompile(`(?m)^.*g\.Any\("/agent/\*".*$`)
	wildcardLoc := wildcardRe.FindStringIndex(src)
	require.NotNilf(t, wildcardLoc, "could not find the /agent/* wildcard registration in %s; this test's assumptions about v2.go's shape are stale", v2GoPath)
	wildcardOffset := wildcardLoc[0]

	// Derived from the same list AdminPathGuard enforces, so the two layers
	// cannot drift: an entry added there with no AdminOnly registration in
	// v2.go fails here.
	for _, entry := range AdminScopedAgentPaths {
		path := entry.Path
		// Match a single source line that registers this exact path (or its
		// "/*" subtree sibling) through v2.AdminOnly, e.g.:
		//   g.Any("/agent/web-settings", agent.Proxy, v2.AdminOnly(runtimePath))
		lineRe := regexp.MustCompile(
			`(?m)^.*g\.Any\("` + regexp.QuoteMeta(path) + `(/\*)?".*v2\.AdminOnly.*$`)
		loc := lineRe.FindStringIndex(src)
		require.NotNilf(t, loc,
			"no admin-gated g.Any registration found for %s in %s; "+
				"if this path was renamed or its gate removed, it is now reachable by any authenticated user",
			path, v2GoPath)
		require.Lessf(t, loc[0], wildcardOffset,
			"%s is admin-gated but registered AFTER the /agent/* wildcard in %s: "+
				"a route pair registered after the wildcard never matches, so %s "+
				"is silently open to any authenticated user, not just admins",
			path, v2GoPath, path)
	}
}

// --- AdminPathGuard: alternate spellings of the admin paths -----------------

// guardedEcho builds the real registration shape from route/v2.go — the
// AdminPathGuard as a pre-router middleware, every admin-scoped pair, then the
// ungated /agent/* wildcard last — behind the /v1/ai group prefix the guard is
// wired with in production.
func guardedEcho(t *testing.T, runtimePath string, proxied *int) *echo.Echo {
	t.Helper()
	e := echo.New()
	e.Pre(AdminPathGuard("/v1/ai"))
	proxy := func(c echo.Context) error { *proxied++; return c.String(http.StatusOK, "proxied") }
	for _, entry := range AdminScopedAgentPaths {
		e.Any("/v1/ai"+entry.Path, proxy, AdminOnly(runtimePath))
		if entry.Subtree {
			e.Any("/v1/ai"+entry.Path+"/*", proxy, AdminOnly(runtimePath))
		}
	}
	e.Any("/v1/ai/agent/*", proxy)
	return e
}

func guardRequest(t *testing.T, e *echo.Echo, method, target string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, target, nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	return rec
}

// encodeFirstLetter rewrites "/a/b/name" into "/a/b/%<hex>ame" — the exact
// shape that dodges Echo's specific route, because Echo matches on RawPath
// whenever the URL carries escapes.
func encodeFirstLetter(p string) string {
	i := strings.LastIndex(p, "/")
	return p[:i+1] + "%" + strings.ToUpper(hex.EncodeToString([]byte{p[i+1]})) + p[i+2:]
}

// TestAdminPathGuardRefusesEncodedAdminPaths is the regression test for the
// bypass. An admin caller is used deliberately: the point is that the request
// never reaches the proxy at all, not that the role check happened to reject it.
func TestAdminPathGuardRefusesEncodedAdminPaths(t *testing.T) {
	us := fakeUserService(t, "admin")
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	for _, entry := range AdminScopedAgentPaths {
		full := "/v1/ai" + entry.Path
		encoded := encodeFirstLetter(full) // /v1/ai/agent/%77eb-settings
		i := strings.LastIndex(full, "/")
		spellings := []string{
			encoded,
			// Double-encoded: %2577 still reads as %77 after one decode pass,
			// which is why the guard decodes repeatedly.
			strings.Replace(encoded, "%", "%25", 1),
			// Dot segment: normalises onto the admin path, but as written it
			// matches the wildcard.
			full[:i] + "/." + full[i:],
		}
		// Trailing slash, for the endpoints with no gated "/*" sibling: today
		// that shape lands on the wildcard too.
		if !entry.Subtree {
			spellings = append(spellings, full+"/")
		}

		for _, target := range spellings {
			proxied := 0
			e := guardedEcho(t, dir, &proxied)
			rec := guardRequest(t, e, http.MethodPut, target)
			require.Equalf(t, http.StatusBadRequest, rec.Code,
				"PUT %s must be refused: it normalises onto the admin-scoped %s",
				target, entry.Path)
			require.Equalf(t, 0, proxied,
				"PUT %s must never reach the agent proxy", target)
		}
	}
}

// TestAdminPathGuardLetsPlainAdminPathsThrough proves the guard is inert for
// the literal spelling: the request still reaches AdminOnly, which still
// answers on the caller's role, exactly as before this guard existed.
func TestAdminPathGuardLetsPlainAdminPathsThrough(t *testing.T) {
	for _, tc := range []struct {
		role string
		code int
	}{{"admin", http.StatusOK}, {"user", http.StatusForbidden}} {
		us := fakeUserService(t, tc.role)
		dir := t.TempDir()
		writeURLFile(t, dir, us.URL)

		for _, entry := range AdminScopedAgentPaths {
			proxied := 0
			e := guardedEcho(t, dir, &proxied)
			rec := guardRequest(t, e, http.MethodGet, "/v1/ai"+entry.Path)
			require.Equalf(t, tc.code, rec.Code,
				"GET /v1/ai%s as %s must reach the gate, not the guard",
				entry.Path, tc.role)

			if entry.Subtree {
				rec = guardRequest(t, e, http.MethodDelete,
					"/v1/ai"+entry.Path+"/abc123")
				require.Equalf(t, tc.code, rec.Code,
					"DELETE /v1/ai%s/abc123 as %s must reach the gate",
					entry.Path, tc.role)
			}
		}
		us.Close()
	}
}

// TestAdminPathGuardIgnoresEncodedNonAdminPaths is the blast-radius guard.
// Attachment and filesystem paths under /agent/* legitimately carry percent
// escapes; the guard must not touch them, which is why route/v2.go does NOT
// clear RawPath for the whole /v1/ai group.
func TestAdminPathGuardIgnoresEncodedNonAdminPaths(t *testing.T) {
	for _, target := range []string{
		"/v1/ai/agent/attachments/my%20photo%20(1).png",
		"/v1/ai/agent/attachments/%E4%B8%AD%E6%96%87.pdf",
		"/v1/ai/agent/files/read?path=%2FDATA%2FDocuments%2Fa%20b.md",
		"/v1/ai/agent/notes/%77hatever",
		"/v1/ai/agent/sessions/abc%2Fdef/messages",
	} {
		proxied := 0
		e := guardedEcho(t, t.TempDir(), &proxied)
		rec := guardRequest(t, e, http.MethodGet, target)
		require.Equalf(t, http.StatusOK, rec.Code,
			"GET %s is not an admin path and must reach the proxy untouched", target)
		require.Equalf(t, 1, proxied, "GET %s must reach the proxy", target)
	}
}

// TestAdminPathGuardIsPrefixScoped documents that the guard only speaks for the
// group it is wired to: an identical path outside /v1/ai is none of its
// business.
func TestAdminPathGuardIsPrefixScoped(t *testing.T) {
	e := echo.New()
	e.Pre(AdminPathGuard("/v1/ai"))
	hit := 0
	e.Any("/other/agent/*", func(c echo.Context) error {
		hit++
		return c.String(http.StatusOK, "ok")
	})
	rec := guardRequest(t, e, http.MethodPut, "/other/agent/%77eb-settings")
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, 1, hit)
}
