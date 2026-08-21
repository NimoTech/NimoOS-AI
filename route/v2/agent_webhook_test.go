package v2

import (
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"testing"

	"github.com/labstack/echo/v4"
)

// The webhook trigger is the one agent route with no JWT: the task's token is
// the entire credential. Two things therefore have to hold in this layer.
//
// First, only the real route pattern may skip authentication. The skipper reads
// echo's MATCHED route pattern rather than the URL, so an encoded spelling of
// the path falls through to the /agent/* wildcard, whose pattern is not this
// one, and still needs a JWT — fail-closed by construction. This test pins that
// the predicate answers on the pattern and nothing else.
func TestIsWebhookTriggerPattern(t *testing.T) {
	for _, tc := range []struct {
		pattern string
		want    bool
	}{
		{"/v1/ai/agent/task-webhook/:token", true},
		// Everything else in the agent surface must keep its JWT.
		{"/v1/ai/agent/*", false},
		{"/v1/ai/agent/tasks", false},
		{"/v1/ai/agent/tasks/:task_id", false},
		{"/v1/ai/agent/task-webhook", false},
		{"/v1/ai/agent/task-webhook/:token/extra", false},
		{"", false},
	} {
		if got := IsWebhookTriggerPattern(tc.pattern); got != tc.want {
			t.Errorf("IsWebhookTriggerPattern(%q) = %v, want %v", tc.pattern, got, tc.want)
		}
	}
}

// Second, an unauthenticated caller must never be able to pick an identity.
// Proxy() reads X-NimoOS-User-ID and hands it downstream as X-User-Id; on a
// JWT-bearing route the middleware overwrites those, but here nothing does, so
// ProxyAnonymous has to delete them before forwarding.
func TestProxyAnonymousStripsIdentityHeaders(t *testing.T) {
	var got http.Header
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		w.WriteHeader(http.StatusAccepted)
	}))
	defer upstream.Close()

	target, _ := url.Parse(upstream.URL)
	h := &AgentHandler{proxy: httputil.NewSingleHostReverseProxy(target)}
	h.available.Store(true)

	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/agent/task-webhook/abc", nil)
	req.Header.Set("X-NimoOS-User-ID", "1")
	req.Header.Set("X-User-Id", "1")
	req.Header.Set("X-User-Name", "admin")
	rec := httptest.NewRecorder()

	if err := h.ProxyAnonymous(e.NewContext(req, rec)); err != nil {
		t.Fatalf("ProxyAnonymous returned %v", err)
	}

	if rec.Code != http.StatusAccepted {
		t.Fatalf("expected the request to reach upstream (202), got %d", rec.Code)
	}
	for _, k := range []string{"X-Nimoos-User-Id", "X-User-Id", "X-User-Name"} {
		if v := got.Get(k); v != "" {
			t.Errorf("identity header %s survived to upstream with value %q", k, v)
		}
	}
}

func TestProxyAnonymousRefusesWhenAgentUnavailable(t *testing.T) {
	h := &AgentHandler{}
	h.available.Store(false)
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/agent/task-webhook/abc", nil)
	rec := httptest.NewRecorder()

	err := h.ProxyAnonymous(e.NewContext(req, rec))

	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 when the agent is down, got %v (rec %d)", err, rec.Code)
	}
}

// The webhook path deliberately sits OUTSIDE the admin-scoped /agent/tasks
// subtree, so that reaching it needs no exception carved into the admin gate.
// Both halves of that claim are load-bearing.
func TestWebhookPathIsNotAdminScopedButTasksStillIs(t *testing.T) {
	for _, raw := range []string{
		"/v1/ai/agent/task-webhook/abc",
		"/v1/ai/agent/task-webhook/abc?x=1",
	} {
		if IsAdminScopedPath("/v1/ai", NormalizeRequestPath(raw)) {
			t.Errorf("%q must not be admin-scoped, or the webhook needs a JWT", raw)
		}
	}
	// A path that merely starts out looking like the webhook but normalizes
	// back into the tasks subtree must still be gated.
	for _, raw := range []string{
		"/v1/ai/agent/tasks",
		"/v1/ai/agent/task-webhook/../tasks",
		"/v1/ai/agent/ta%73ks/abc",
	} {
		if !IsAdminScopedPath("/v1/ai", NormalizeRequestPath(raw)) {
			t.Errorf("%q must stay admin-scoped", raw)
		}
	}
}
