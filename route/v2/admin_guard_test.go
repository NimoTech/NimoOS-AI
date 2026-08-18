package v2

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func TestNormalizeRequestPathDecodesAndCleans(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		{"/v1/ai/agent/tasks", "/v1/ai/agent/tasks"},
		{"/v1/ai/agent/ta%73ks", "/v1/ai/agent/tasks"},
		{"/v1/ai/agent/ta%2573ks", "/v1/ai/agent/tasks"},   // double-encoded
		{"/v1/ai/agent/ta%252573ks", "/v1/ai/agent/tasks"}, // triple
		{"/v1/ai/agent/tasks%2fabc", "/v1/ai/agent/tasks/abc"},
		{"/v1/ai/agent/tasks%2Fabc", "/v1/ai/agent/tasks/abc"}, // uppercase hex
		{"/v1/ai/agent/tasks/", "/v1/ai/agent/tasks"},
		{"//v1/ai//agent//tasks", "/v1/ai/agent/tasks"},
		{"/v1/ai/agent/./tasks", "/v1/ai/agent/tasks"},
		{"/v1/ai/agent/toolbox/../tasks", "/v1/ai/agent/tasks"},
		{"v1/ai/agent/tasks", "/v1/ai/agent/tasks"}, // no leading slash
		// A stray '%' cannot be decoded; the function must still return
		// something judgable rather than erroring or looping.
		{"/v1/ai/agent/ta%zzks", "/v1/ai/agent/ta%zzks"},
		{"/v1/ai/agent/notes/settings%", "/v1/ai/agent/notes/settings%"},
	} {
		require.Equalf(t, tc.want, NormalizeRequestPath(tc.in), "input %q", tc.in)
	}
}

func TestIsAdminScopedPath(t *testing.T) {
	admin := []string{
		"/v1/ai/agent/tasks",
		"/v1/ai/agent/tasks/abc/runs",
		"/v1/ai/agent/toolbox",
		"/v1/ai/agent/toolbox/install",
		"/v1/ai/agent/shell-allowlist",
		"/v1/ai/agent/notes/settings",
		"/v1/ai/agent/notes/dir-info",
		"/v1/ai/agent/channels/instances/xyz",
	}
	for _, p := range admin {
		require.Truef(t, IsAdminScopedPath("/v1/ai", p), "%q must be admin-scoped", p)
	}

	perUser := []string{
		"/v1/ai/agent/sessions",
		"/v1/ai/agent/notes",         // the notes surface itself is per-user
		"/v1/ai/agent/notes/abc",     // one note
		"/v1/ai/agent/tasksomething", // prefix but not a path segment
		"/v1/ai/agent/toolboxes",     // ditto
		"/v1/ai/agent/channels/pairing-code",
		"/v1/ai/agent/lark/binding",
		"/v1/ai/agent/health",
		"/v1/ai/search/query",
	}
	for _, p := range perUser {
		require.Falsef(t, IsAdminScopedPath("/v1/ai", p), "%q must NOT be admin-scoped", p)
	}
}

// The guard has to make its decision from the request URL, never from the
// matched route — that independence is the whole point.
func TestAdminPathGuardIgnoresTheMatchedRoute(t *testing.T) {
	us := fakeUserService(t, "user")
	defer us.Close()
	dir := t.TempDir()
	writeURLFile(t, dir, us.URL)

	e := echo.New()
	reached := 0
	// Registered ONLY as a wildcard, exactly how an encoded path is routed.
	e.Use(AdminPathGuard(dir, "/v1/ai"))
	e.Any("/v1/ai/agent/*", func(c echo.Context) error {
		reached++
		return c.String(http.StatusOK, "ok")
	})

	req := httptest.NewRequest(http.MethodGet, "/v1/ai/agent/ta%73ks", nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusForbidden, rec.Code)
	require.Equal(t, 0, reached, "the handler must not run")

	// A per-user path through the same wildcard is untouched.
	req = httptest.NewRequest(http.MethodGet, "/v1/ai/agent/sessions", nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec = httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, 1, reached)
}

// Fail closed: if UserService cannot be reached, an admin-scoped path is 503,
// never proxied.
func TestAdminPathGuardFailsClosed(t *testing.T) {
	e := echo.New()
	reached := 0
	e.Use(AdminPathGuard(t.TempDir(), "/v1/ai")) // no user-service.url
	e.Any("/v1/ai/agent/*", func(c echo.Context) error {
		reached++
		return c.String(http.StatusOK, "ok")
	})

	req := httptest.NewRequest(http.MethodGet, "/v1/ai/agent/tasks%2fabc", nil)
	req.Header.Set("Authorization", "Bearer tok")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	require.Equal(t, http.StatusServiceUnavailable, rec.Code)
	require.Equal(t, 0, reached)
}

// The M6 draft endpoint lives under /agent/tasks, so it inherits the admin
// gate through subtree matching rather than its own list entry. This test
// pins that inheritance: it hands out shell prefixes and fs_write roots just
// like the rest of the tasks API, so it must never become reachable by a
// non-admin — including through the percent-encoded spellings that motivated
// AdminPathGuard in the first place.
func TestDraftEndpointIsAdminScoped(t *testing.T) {
	for _, raw := range []string{
		"/v1/ai/agent/tasks/draft-from-session",
		"/v1/ai/agent/ta%73ks/draft-from-session",
		"/v1/ai/agent/tasks%2fdraft-from-session",
		"/v1/ai/agent/tasks/../tasks/draft-from-session",
		"/v1/ai/agent//tasks//draft-from-session",
	} {
		if got := IsAdminScopedPath("/v1/ai", NormalizeRequestPath(raw)); !got {
			t.Errorf("IsAdminScopedPath(%q) = false, want true", raw)
		}
	}
}

func TestLarkChannelEndpointsAreAdminScoped(t *testing.T) {
	for _, raw := range []string{
		"/v1/ai/agent/channels/lark",
		"/v1/ai/agent/channels/la%72k",
		"/v1/ai/agent/channels/lark/../lark",
	} {
		if !IsAdminScopedPath("/v1/ai", NormalizeRequestPath(raw)) {
			t.Errorf("%q must be admin-scoped: enabling a channel configures the box", raw)
		}
	}
}

func TestDraftEndpointNeedsMCPTicket(t *testing.T) {
	cases := []struct {
		method, path string
		want         bool
	}{
		{http.MethodPost, "/v1/ai/agent/tasks/draft-from-session", true},
		{http.MethodPost, "/v1/ai/agent/run", true},
		{http.MethodPost, "/v1/ai/agent/tasks/abc/run", true},
		{http.MethodGet, "/v1/ai/agent/tasks/draft-from-session", false},
		{http.MethodPost, "/v1/ai/agent/tasks", false},
		// Same normalization the admin gate relies on: an encoded or traversed
		// spelling of the real endpoint still needs its ticket.
		{http.MethodPost, "/v1/ai/agent/ta%73ks/draft-from-session", true},
		{http.MethodPost, "/v1/ai/agent/tasks%2fdraft-from-session", true},
		{http.MethodPost, "/v1/ai/agent/tasks/../tasks/draft-from-session", true},
		// Ends with the endpoint's path but is not the endpoint.
		{http.MethodPost, "/v1/ai/agent/foo/agent/tasks/draft-from-session", false},
		// A different endpoint whose name merely ends the same way.
		{http.MethodPost, "/v1/ai/agent/tasks/x-draft-from-session", false},
	}
	for _, c := range cases {
		req := httptest.NewRequest(c.method, c.path, nil)
		if got := needsMCPTicket(req); got != c.want {
			t.Errorf("needsMCPTicket(%s %s) = %v, want %v", c.method, c.path, got, c.want)
		}
	}
}
