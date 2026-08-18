package v2

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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
