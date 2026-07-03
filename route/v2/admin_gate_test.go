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
