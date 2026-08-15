package v2

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

// --- Task 22, Part 1: migration sweep for pre-existing servers ---

// After this feature ships, a server that existed before it has no
// mcp_server_runtime row at all, so the model's L1 catalogue shows it as
// "not yet probed" forever. The migration sweep is simply the same
// probe-and-persist flow a newly-added server gets, run once against every
// enabled server missing a runtime row — no new mechanism.
func TestMigrationProbesServersWithoutRuntimeRow(t *testing.T) {
	svc := mcpTestSvc(t)
	m := &service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://x/mcp",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}

	agent := probeAgentStub(t, `{
		"ok": true, "tool_count": 1, "tools": ["search_repos"],
		"server_info": {"name": "github-mcp-server"},
		"tool_metas": [{"name": "search_repos", "schema_hash": "sh1", "desc_hash": "dh1"}],
		"ttl_sec": 300
	}`)
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)
	if err := h.migrateBackfillIdentityCards(); err != nil {
		t.Fatalf("migrateBackfillIdentityCards: %v", err)
	}

	r, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if r == nil {
		t.Fatal("expected the migration sweep to have probed and persisted a runtime row")
	}
	if r.ProbeState != "ok" {
		t.Fatalf("probe_state = %q, want ok", r.ProbeState)
	}
	if r.Handle != "github" {
		t.Fatalf("handle = %q, want github", r.Handle)
	}
}

// Idempotent: restarting the process must never re-probe a server that
// already has an identity card. Only the LEFT JOIN gap (no runtime row at
// all) is migration work.
func TestMigrationSkipsServersThatAlreadyHaveRuntime(t *testing.T) {
	svc := mcpTestSvc(t)
	already := &service.McpServer{
		UserID: "u1", Name: "already-probed", Transport: "http", URL: "https://already/mcp",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(already); err != nil {
		t.Fatalf("create already: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{
		ServerID: already.ID, Handle: "already", Summary: "pre-existing", TTLSec: 300,
	}, []service.ToolMeta{{Name: "noop"}}, "[]"); err != nil {
		t.Fatalf("seed runtime row: %v", err)
	}

	pending := &service.McpServer{
		UserID: "u1", Name: "never-probed", Transport: "http", URL: "https://pending/mcp",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(pending); err != nil {
		t.Fatalf("create pending: %v", err)
	}

	var calls int32
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok": true, "tool_metas": [], "tools": []}`))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)
	if err := h.migrateBackfillIdentityCards(); err != nil {
		t.Fatalf("migrateBackfillIdentityCards: %v", err)
	}

	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("agent received %d probe requests, want exactly 1 (only the server missing a runtime row)", got)
	}

	r, err := svc.MCPRuntime().Get(already.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get(already): %v", err)
	}
	if r == nil || r.Summary != "pre-existing" {
		t.Fatalf("the already-probed server's runtime row was touched by the sweep: %+v", r)
	}

	pr, err := svc.MCPRuntime().Get(pending.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get(pending): %v", err)
	}
	if pr == nil || pr.ProbeState != "ok" {
		t.Fatalf("expected the pending server to have been probed, got %+v", pr)
	}
}

// A single stdio probe can take up to ~120s (see probeAndPersist's timeout
// comment); serially probing ten of those on the startup path would hang the
// service for ~20 minutes if it blocked. StartMigrationBackfill must
// therefore hand the work to a background goroutine and return immediately,
// and that goroutine must probe candidates one at a time — never all at
// once, which would spawn many child processes concurrently and could
// saturate a NAS's disk/network.
func TestMigrationIsAsyncAndDoesNotBlockStartup(t *testing.T) {
	svc := mcpTestSvc(t)
	var servers []*service.McpServer
	for i := 0; i < 2; i++ {
		m := &service.McpServer{
			UserID: "u1", Name: fmt.Sprintf("srv-%d", i), Transport: "http",
			URL: fmt.Sprintf("https://x/%d", i), Args: "[]", Env: "{}", Enabled: true,
		}
		if err := svc.MCP().CreateMcpServer(m); err != nil {
			t.Fatalf("create: %v", err)
		}
		servers = append(servers, m)
	}

	// The stub blocks every request on <-release until the test explicitly
	// lets it through, and reports arrivals on `arrived` and concurrency on
	// `maxInFlight`. This lets the test prove ordering/serialism from actual
	// synchronized events instead of guessing at sleep durations.
	arrived := make(chan struct{}, len(servers))
	release := make(chan struct{})
	var inFlight int32
	var maxInFlight int32
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&inFlight, 1)
		for {
			old := atomic.LoadInt32(&maxInFlight)
			if n <= old || atomic.CompareAndSwapInt32(&maxInFlight, old, n) {
				break
			}
		}
		arrived <- struct{}{}
		<-release
		atomic.AddInt32(&inFlight, -1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok": true, "tool_metas": [], "tools": []}`))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)

	// StartMigrationBackfill must return before either probe (both currently
	// wedged on <-release, since we haven't sent anything to it yet) has any
	// chance to complete — proving the sweep runs on its own goroutine
	// rather than on the caller's. The 2s ceiling is generous: legitimate
	// code returns in microseconds here, so this is not a timing-sensitive
	// assertion, only a backstop against a genuine hang.
	returned := make(chan struct{})
	go func() {
		h.StartMigrationBackfill()
		close(returned)
	}()
	select {
	case <-returned:
	case <-time.After(2 * time.Second):
		t.Fatal("StartMigrationBackfill did not return promptly — it must launch a goroutine, not block its caller")
	}

	// First candidate has reached the stub and is now blocked there.
	select {
	case <-arrived:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for the first probe to reach the agent stub")
	}
	// The second candidate must NOT have started yet: the sweep is serial,
	// and the first request is still held open on <-release, so there is no
	// race here — a second arrival before we release the first can only mean
	// the sweep fired both probes concurrently.
	select {
	case <-arrived:
		t.Fatal("a second probe started before the first one finished — the sweep must be serial")
	default:
	}
	release <- struct{}{} // let the first probe finish

	select {
	case <-arrived:
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for the second probe to reach the agent stub")
	}
	release <- struct{}{} // let the second probe finish

	if got := atomic.LoadInt32(&maxInFlight); got > 1 {
		t.Fatalf("max concurrent probes = %d, want 1 (serial fan-out)", got)
	}

	// Both servers must eventually get a runtime row. The ordering/async
	// claims above are all proven by channel synchronization; this final
	// wait only covers the brief residual gap between "the HTTP client
	// finished reading the response" and "SaveSuccess committed its write",
	// which has no externally observable event to synchronize on — a short
	// bounded poll (not a single fixed-guess sleep) is the standard way to
	// wait that out.
	deadline := time.Now().Add(2 * time.Second)
	for _, m := range servers {
		for {
			r, err := svc.MCPRuntime().Get(m.ID)
			if err != nil {
				t.Fatalf("MCPRuntime().Get: %v", err)
			}
			if r != nil && r.ProbeState == "ok" {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("server %d never got a persisted runtime row from the migration sweep", m.ID)
			}
			time.Sleep(5 * time.Millisecond)
		}
	}
}

// --- Task 22, Part 2: closing the config-invalidation gap found by review ---

func doUpdate(t *testing.T, h *MCPHandler, id int64, uid, body string) *httptest.ResponseRecorder {
	t.Helper()
	e := echo.New()
	req := httptest.NewRequest(http.MethodPut, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", uid)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues(fmt.Sprintf("%d", id))
	if err := h.Update(c); err != nil {
		t.Fatalf("Update: %v", err)
	}
	return rec
}

// Task 13 deleted Python's _fingerprint (which used to invalidate the
// cached tool listing the instant url/headers/command changed) and nothing
// replaced it — Update wrote only mcp_servers, never touching
// mcp_server_runtime, so a stale listing kept serving for up to
// SCHEMA_TTL_MAX after an edit. A transport-relevant change (here: url) must
// zero listed_at/ttl_sec so the next Runtime GET re-probes.
func TestUpdateInvalidatesRuntimeConfigOnURLChange(t *testing.T) {
	svc := mcpTestSvc(t)
	m := &service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://old.example.com/mcp",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{
		ServerID: m.ID, Handle: "github", Summary: "old", TTLSec: 300,
	}, []service.ToolMeta{{Name: "noop"}}, "[]"); err != nil {
		t.Fatalf("seed runtime row: %v", err)
	}
	if r, _ := svc.MCPRuntime().Get(m.ID); r == nil || r.ListedAt == 0 {
		t.Fatalf("test setup: expected a seeded runtime row with a non-zero listed_at, got %+v", r)
	}

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	rec := doUpdate(t, h, m.ID, "u1", `{"url":"https://new.example.com/mcp"}`)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("update code = %d, body = %s", rec.Code, rec.Body.String())
	}

	r, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if r == nil {
		t.Fatal("expected the runtime row to still exist")
	}
	if r.ListedAt != 0 || r.TTLSec != 0 {
		t.Fatalf("listed_at=%d ttl_sec=%d, want both zeroed after a URL (transport-relevant) change", r.ListedAt, r.TTLSec)
	}
}

// Renaming a server or toggling it off and on must NEVER invalidate its
// cached listing — a load-bearing property throughout this plan (the
// approval gates key on identity_fp, not updated_at, for exactly this
// reason). A naive "invalidate on any Update" fix would reintroduce that
// class of bug on the caching side.
func TestUpdateDoesNotInvalidateRuntimeOnRenameOrToggle(t *testing.T) {
	svc := mcpTestSvc(t)
	m := &service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://x.example.com/mcp",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{
		ServerID: m.ID, Handle: "github", Summary: "unchanged", TTLSec: 300,
	}, []service.ToolMeta{{Name: "noop"}}, "[]"); err != nil {
		t.Fatalf("seed runtime row: %v", err)
	}
	before, err := svc.MCPRuntime().Get(m.ID)
	if err != nil || before == nil || before.ListedAt == 0 {
		t.Fatalf("test setup: expected a seeded runtime row with a non-zero listed_at, got %+v, err=%v", before, err)
	}

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	rec := doUpdate(t, h, m.ID, "u1", `{"name":"github (renamed)","enabled":false}`)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("update code = %d, body = %s", rec.Code, rec.Body.String())
	}
	rec2 := doUpdate(t, h, m.ID, "u1", `{"enabled":true}`)
	if rec2.Code != http.StatusNoContent {
		t.Fatalf("update(re-enable) code = %d, body = %s", rec2.Code, rec2.Body.String())
	}

	after, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if after == nil {
		t.Fatal("expected the runtime row to still exist")
	}
	if after.ListedAt != before.ListedAt || after.TTLSec != before.TTLSec {
		t.Fatalf("listed_at/ttl_sec changed after a rename+toggle (name/enabled must never invalidate): before=%+v after=%+v", before, after)
	}
}
