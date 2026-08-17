package route

import (
	"crypto/ecdsa"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/NimoTech/NimoOS-AI/pkg/config"
	"github.com/NimoTech/NimoOS-Common/external"
	"github.com/NimoTech/NimoOS-Common/utils/jwt"
)

// The admin gate on /v1/ai/agent/tasks[/*] exercised through the REAL router
// InitV2Router builds, not a hand-mirrored registration: the wildcard
// /agent/* sits right after these two routes, and whether Echo prefers the
// static segment is exactly what the gate depends on. Route/v2's
// admin_gate_test.go pins the middleware itself; this pins the wiring.
//
// A JWT is required to reach the gate at all (the JWT middleware runs first),
// so the test mints its own: the fake user service serves both the JWKS the
// middleware validates against and the /v1/users/current lookup AdminOnly
// reads the caller's role from.
type fakeUserSvc struct {
	server *httptest.Server
	role   atomic.Value // string
	calls  atomic.Int32
}

func newFakeUserSvc(t *testing.T, jwks []byte) *fakeUserSvc {
	t.Helper()
	f := &fakeUserSvc{}
	f.role.Store("user")
	mux := http.NewServeMux()
	// "/"+JWKSPath: the constant has no leading slash (it is used with
	// url.JoinPath), and a ServeMux pattern without one is read as a HOST
	// pattern, which would never match.
	mux.HandleFunc("/"+jwt.JWKSPath, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(jwks)
	})
	mux.HandleFunc("/v1/users/current", func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":{"role":"` + f.role.Load().(string) + `"}}`))
	})
	f.server = httptest.NewServer(mux)
	t.Cleanup(f.server.Close)
	return f
}

type proxied struct {
	paths  chan string
	server *httptest.Server
}

func newFakeAgent(t *testing.T) *proxied {
	t.Helper()
	p := &proxied{paths: make(chan string, 32)}
	p.server = httptest.NewServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/agent/health" {
				w.WriteHeader(http.StatusOK)
				return
			}
			p.paths <- r.URL.Path
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"proxied":true}`))
		}))
	t.Cleanup(p.server.Close)
	return p
}

// lastPath drains the recorded proxy hits and returns the last one, or "" when
// nothing reached the agent (i.e. the request was answered before the proxy).
func (p *proxied) lastPath() string {
	last := ""
	for {
		select {
		case v := <-p.paths:
			last = v
		default:
			return last
		}
	}
}

// One key pair for the whole binary. `external.GetPublicKey` caches the key it
// fetched for 10 seconds in a package-level variable, so a per-test key pair
// would have the second test validating its token against the first test's
// key. Sharing one pair makes that cache a no-op instead of a trap.
var (
	testKeyOnce sync.Once
	testPriv    *ecdsa.PrivateKey
	testJWKS    []byte
	testToken   string
)

func testCredentials(t *testing.T) (*ecdsa.PrivateKey, []byte, string) {
	t.Helper()
	testKeyOnce.Do(func() {
		priv, pub, err := jwt.GenerateKeyPair()
		if err != nil {
			t.Fatal(err)
		}
		jwks, err := jwt.GenerateJwksJSON(pub)
		if err != nil {
			t.Fatal(err)
		}
		token, err := jwt.GetAccessToken("tester", priv, 7)
		if err != nil {
			t.Fatal(err)
		}
		testPriv, testJWKS, testToken = priv, jwks, token
	})
	return testPriv, testJWKS, testToken
}

func newRealRouter(t *testing.T) (http.Handler, *fakeUserSvc, *proxied, string) {
	t.Helper()
	root := t.TempDir()
	runtimePath := filepath.Join(root, "run")
	if err := os.MkdirAll(runtimePath, 0o755); err != nil {
		t.Fatal(err)
	}

	_, jwks, token := testCredentials(t)
	users := newFakeUserSvc(t, jwks)
	// No trailing newline: external.getAddress returns the file's bytes
	// verbatim (no TrimSpace), and a newline inside the URL makes the JWKS
	// fetch fail — which is how the real runtime files are written too.
	if err := os.WriteFile(
		filepath.Join(runtimePath, external.UserServiceAddressFilename),
		[]byte(users.server.URL), 0o644); err != nil {
		t.Fatal(err)
	}

	agent := newFakeAgent(t)

	config.Cfg = &config.Config{
		RuntimePath: runtimePath,
		DataPath:    root,
		AgentURL:    agent.server.URL,
		OllamaURL:   "http://127.0.0.1:1",
		OpenVINOURL: "http://127.0.0.1:1",
	}
	// svc is deliberately nil: every handler this test exercises guards on
	// `h.svc != nil` (provider-credential injection, the blacklist header, the
	// lazy skills runtime view), and a routing test has no business standing up
	// a database and a master key to reach the router's route table.
	h := InitV2Router(nil, runtimePath, agent.server.URL,
		config.Cfg.OllamaURL, config.Cfg.OpenVINOURL)

	// StartHealthMonitor flips `available` from its own goroutine; the agent
	// proxy answers 503 until it has.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/v1/ai/agent/health", nil)
		req.Header.Set("Authorization", "Bearer "+token)
		h.ServeHTTP(rec, req)
		if rec.Code == http.StatusOK {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	return h, users, agent, token
}

func call(t *testing.T, h http.Handler, method, path, token string) int {
	t.Helper()
	req := httptest.NewRequest(method, path, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec.Code
}

var taskEndpoints = []struct{ method, path string }{
	{http.MethodGet, "/v1/ai/agent/tasks"},
	{http.MethodPost, "/v1/ai/agent/tasks"},
	{http.MethodGet, "/v1/ai/agent/tasks/notify-targets"},
	{http.MethodGet, "/v1/ai/agent/tasks/abc123"},
	{http.MethodPut, "/v1/ai/agent/tasks/abc123"},
	{http.MethodDelete, "/v1/ai/agent/tasks/abc123"},
	{http.MethodPost, "/v1/ai/agent/tasks/abc123/run"},
	{http.MethodGet, "/v1/ai/agent/tasks/abc123/runs"},
	{http.MethodPost, "/v1/ai/agent/tasks/abc123/preauth/from-denied"},
}

func TestRealRouterTaskEndpointsRejectNonAdmin(t *testing.T) {
	h, users, agent, token := newRealRouter(t)
	users.role.Store("user")

	for _, e := range taskEndpoints {
		if code := call(t, h, e.method, e.path, token); code != http.StatusForbidden {
			t.Fatalf("%s %s: got %d, want 403 (the wildcard swallowed the gated route)",
				e.method, e.path, code)
		}
		if p := agent.lastPath(); p != "" {
			t.Fatalf("%s %s reached the agent at %s despite the gate",
				e.method, e.path, p)
		}
	}
}

func TestRealRouterTaskEndpointsPassAdminThrough(t *testing.T) {
	h, users, agent, token := newRealRouter(t)
	users.role.Store("admin")

	for _, e := range taskEndpoints {
		if code := call(t, h, e.method, e.path, token); code != http.StatusOK {
			t.Fatalf("%s %s: got %d, want 200 for an admin", e.method, e.path, code)
		}
		// And the agent saw it with the /v1/ai prefix stripped, i.e. it went
		// through the same proxy the wildcard uses.
		want := e.path[len("/v1/ai"):]
		if p := agent.lastPath(); p != want {
			t.Fatalf("%s %s: agent saw %q, want %q", e.method, e.path, p, want)
		}
	}
}

func TestRealRouterPerUserAgentEndpointsAreNotGated(t *testing.T) {
	h, users, agent, token := newRealRouter(t)
	users.role.Store("user") // a non-admin must keep full use of their own data

	for _, e := range []struct{ method, path string }{
		{http.MethodGet, "/v1/ai/agent/sessions"},
		{http.MethodPost, "/v1/ai/agent/sessions"},
		{http.MethodGet, "/v1/ai/agent/lark/binding"},
		{http.MethodGet, "/v1/ai/agent/notes"},
		{http.MethodGet, "/v1/ai/agent/context-usage"},
	} {
		if code := call(t, h, e.method, e.path, token); code != http.StatusOK {
			t.Fatalf("%s %s: got %d, want 200 — per-user endpoints must not be "+
				"caught by the tasks gate", e.method, e.path, code)
		}
		if p := agent.lastPath(); p != e.path[len("/v1/ai"):] {
			t.Fatalf("%s %s did not reach the agent (saw %q)", e.method, e.path, p)
		}
	}
	if users.calls.Load() != 0 {
		t.Fatalf("the admin gate was consulted %d time(s) for ungated endpoints",
			users.calls.Load())
	}
}

func TestRealRouterTaskEndpointsRequireAJWT(t *testing.T) {
	h, _, agent, _ := newRealRouter(t)
	if code := call(t, h, http.MethodGet, "/v1/ai/agent/tasks", ""); code != http.StatusBadRequest &&
		code != http.StatusUnauthorized {
		t.Fatalf("no token: got %d, want 400/401", code)
	}
	if p := agent.lastPath(); p != "" {
		t.Fatalf("an unauthenticated request reached the agent at %s", p)
	}
}
