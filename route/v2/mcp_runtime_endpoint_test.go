package v2

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/NimoTech/NimoOS-AI/pkg/crypto"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

// mcpRuntimeTestSvc is mcpTestSvc (mcp_test.go) plus the raw *sql.DB handle,
// needed here to backdate listed_at/cooldown_until directly — SaveSuccess
// always stamps listed_at with time.Now(), so a "TTL already expired" or
// "in cooldown" fixture can only be built by writing the column directly
// after the fact (same pattern as service/mcp_runtime_test.go:150).
func mcpRuntimeTestSvc(t *testing.T) (service.Services, *sql.DB) {
	t.Helper()
	dir := t.TempDir()
	db, err := service.NewDB(filepath.Join(dir, "ai.db"))
	if err != nil {
		t.Fatalf("db: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	mk, err := crypto.LoadOrCreate(filepath.Join(dir, "master.key"))
	if err != nil {
		t.Fatalf("mk: %v", err)
	}
	return service.NewServicesForTest(db, mk), db
}

// TestRuntimeTriggersRefreshWhenTTLExpiredButDoesNotWait is the key invariant
// of the TTL self-check: when the stored listing has expired, Runtime kicks
// off a background probe but does NOT wait for it — waiting would violate
// requirement ① (zero added latency at run start). This request must ship
// the current (stale) listing; the next Runtime GET gets whatever the
// refresh produced.
//
// The fake agent sleeps 2s before answering, which would make the test
// itself slow (and flaky under -race) if the handler ever blocked on it. The
// bound below is nowhere near that 2s, so a regression that makes Runtime
// wait for the probe fails loudly rather than merely running slower.
func TestRuntimeTriggersRefreshWhenTTLExpiredButDoesNotWait(t *testing.T) {
	svc, db := mcpRuntimeTestSvc(t)
	ts := NewTicketStore(time.Minute)

	probeStarted := make(chan struct{})
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(probeStarted)
		time.Sleep(2 * time.Second)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"tool_count":1,"tools":["a"],"protocol_era":"legacy",
			"ttl_sec":600,"tool_metas":[{"name":"a","schema_hash":"h1","desc_hash":"d1"}]}`))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, ts, NewRunTokenStore(time.Minute), agent.URL)

	if err := svc.MCP().CreateMcpServer(&service.McpServer{
		UserID: "u1", Name: "gh", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Headers: "", Enabled: true,
	}); err != nil {
		t.Fatalf("create: %v", err)
	}
	rows, _ := svc.MCP().ListMcpServers("u1")
	serverID := rows[0].ID

	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{ServerID: serverID, TTLSec: 1},
		[]service.ToolMeta{{Name: "a", SchemaHash: "h0", DescHash: "d0"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	// SaveSuccess always stamps listed_at = time.Now(); backdate it directly
	// so the 1s TTL is already expired without a real sleep in the test.
	oldListedAt := time.Now().Unix() - 1000
	if _, err := db.Exec(`UPDATE mcp_server_runtime SET listed_at=? WHERE server_id=?`, oldListedAt, serverID); err != nil {
		t.Fatalf("backdate listed_at: %v", err)
	}

	tok := ts.Mint("u1")
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Agent-MCP-Ticket", tok)
	rec := httptest.NewRecorder()
	e := echo.New()

	start := time.Now()
	if err := h.Runtime(e.NewContext(req, rec)); err != nil {
		t.Fatalf("runtime: %v", err)
	}
	elapsed := time.Since(start)
	if elapsed > time.Second {
		t.Fatalf("Runtime blocked on the TTL refresh: took %v (fake probe sleeps 2s) — "+
			"the self-check must fire-and-forget, never wait", elapsed)
	}

	var out struct {
		Servers []struct {
			ListedAt int64 `json:"listed_at"`
		} `json:"servers"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if len(out.Servers) != 1 || out.Servers[0].ListedAt != oldListedAt {
		t.Fatalf("this round must ship the stale listed_at %d (next round gets the refresh), got %+v",
			oldListedAt, out.Servers)
	}

	// Prove the async path actually reaches the real probe rather than being
	// swallowed by a double MarkProbing claim (probeAndPersist claims the
	// single-flight lock itself; claiming it again in the handler first would
	// make that inner claim fail and probeAndPersist silently no-op).
	select {
	case <-probeStarted:
	case <-time.After(time.Second):
		t.Fatal("background probe never reached the agent — the TTL self-check did not fire at all")
	}

	// Poll (rather than synchronize on the fake server's handler returning)
	// for the persisted row to show a fresh listing: the HTTP client inside
	// probeAndPersist still has to read the body and call SaveSuccess after
	// the server-side handler returns, so asserting immediately after the
	// server sends its response would race the client-side write.
	deadline := time.Now().Add(4 * time.Second)
	var rt *service.McpServerRuntime
	for time.Now().Before(deadline) {
		var err error
		rt, err = svc.MCPRuntime().Get(serverID)
		if err != nil {
			t.Fatalf("get runtime after probe: %v", err)
		}
		if rt != nil && rt.ListedAt != oldListedAt {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("background probe never persisted a fresh listing within the deadline — " +
		"it was silently swallowed (likely a double MarkProbing claim) or never ran")
}

// TestRuntimeSkipsRefreshWhenInCooldown asserts the circuit breaker actually
// gates the self-check: an expired TTL must NOT trigger a probe while the
// server is still in its backoff cooldown window, or the breaker is
// decorative.
func TestRuntimeSkipsRefreshWhenInCooldown(t *testing.T) {
	svc, db := mcpRuntimeTestSvc(t)
	ts := NewTicketStore(time.Minute)

	var probeHit atomic.Bool
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		probeHit.Store(true)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"tool_count":1,"tools":["a"]}`))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, ts, NewRunTokenStore(time.Minute), agent.URL)

	if err := svc.MCP().CreateMcpServer(&service.McpServer{
		UserID: "u1", Name: "gh", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Headers: "", Enabled: true,
	}); err != nil {
		t.Fatalf("create: %v", err)
	}
	rows, _ := svc.MCP().ListMcpServers("u1")
	serverID := rows[0].ID

	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{ServerID: serverID, TTLSec: 1},
		[]service.ToolMeta{{Name: "a", SchemaHash: "h", DescHash: "d"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	// Expire the listing AND put the server into an active cooldown — the
	// breaker must win regardless of how stale the listing is.
	now := time.Now().Unix()
	if _, err := db.Exec(`UPDATE mcp_server_runtime SET listed_at=?, cooldown_until=? WHERE server_id=?`,
		now-1000, now+3600, serverID); err != nil {
		t.Fatalf("seed cooldown: %v", err)
	}

	tok := ts.Mint("u1")
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Agent-MCP-Ticket", tok)
	rec := httptest.NewRecorder()
	e := echo.New()
	if err := h.Runtime(e.NewContext(req, rec)); err != nil {
		t.Fatalf("runtime: %v", err)
	}

	// Give any wrongly-spawned goroutine a chance to reach the fake agent
	// before asserting it never did.
	time.Sleep(150 * time.Millisecond)
	if probeHit.Load() {
		t.Fatal("a server in cooldown was probed — the circuit breaker is decorative")
	}
}

// TestRuntimeIncludesWriteTokenAndApprovals asserts the response carries the
// run-scoped write token and the pre-filtered (already-gated) approval set
// in the SAME response as the server list — zero extra round-trips is part
// of requirement ①.
func TestRuntimeIncludesWriteTokenAndApprovals(t *testing.T) {
	svc, _ := mcpRuntimeTestSvc(t)
	ts := NewTicketStore(time.Minute)
	runTokens := NewRunTokenStore(time.Minute)
	h := NewMCPHandler(svc, ts, runTokens, "http://127.0.0.1:1")

	if err := svc.MCP().CreateMcpServer(&service.McpServer{
		UserID: "u1", Name: "gh", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Headers: "", Enabled: true,
	}); err != nil {
		t.Fatalf("create: %v", err)
	}
	rows, _ := svc.MCP().ListMcpServers("u1")
	serverID := rows[0].ID

	const identityFP = "fp-1"
	// TTL far in the future: this listing is fresh, so no background probe
	// fires and the (deliberately unreachable) agentURL above is never hit.
	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{
		ServerID: serverID, TTLSec: 3600, IdentityFP: identityFP,
	}, []service.ToolMeta{{Name: "create_issue", SchemaHash: "h1", DescHash: "d1"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	// A server-level '*' approval whose identity_fp matches the current
	// runtime row passes all four EffectiveApprovals gates.
	if err := svc.MCPApprovals().PutServerLevel(serverID, identityFP); err != nil {
		t.Fatalf("put approval: %v", err)
	}

	tok := ts.Mint("u1")
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Agent-MCP-Ticket", tok)
	rec := httptest.NewRecorder()
	e := echo.New()
	if err := h.Runtime(e.NewContext(req, rec)); err != nil {
		t.Fatalf("runtime: %v", err)
	}

	var out struct {
		Approvals []struct {
			ServerID int64  `json:"server_id"`
			ToolName string `json:"tool_name"`
		} `json:"approvals"`
		WriteToken string `json:"write_token"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if out.WriteToken == "" {
		t.Fatal("write_token missing — Python has no credential to record a mid-run " +
			"'don't ask again' click, since the one-time bootstrap ticket is already consumed")
	}
	if len(out.Approvals) != 1 || out.Approvals[0].ServerID != serverID || out.Approvals[0].ToolName != "*" {
		t.Fatalf("expected the one effective approval to ship in the same response, got %+v", out.Approvals)
	}

	// The token must actually resolve to this run's user — proving it was
	// minted through the real RunTokenStore, not just echoed as a literal.
	uid, ok := runTokens.Resolve(out.WriteToken)
	if !ok || uid != "u1" {
		t.Fatalf("write_token does not resolve to the run's user: uid=%q ok=%v", uid, ok)
	}
}
