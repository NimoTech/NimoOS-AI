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
// delete BOTH the runtime row and the schemas row outright — a column-only
// reset (listed_at=0, ttl_sec=0) would leave identity_fp, tools_json,
// protocol_mode and the separate mcp_server_schemas row all pointing at the
// OLD server, which is exactly what let a stale "don't ask again" approval
// keep working after a config change (the config gate compares against
// identity_fp, which only a column reset would leave untouched). Deleting
// both rows makes the server read as never-probed everywhere at once, and
// void any approval that was effective under the old identity.
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
		ServerID: m.ID, Handle: "github", Summary: "old", IdentityFP: "old-identity-fp", TTLSec: 300,
	}, []service.ToolMeta{{Name: "noop", SchemaHash: "sh1"}}, "[]"); err != nil {
		t.Fatalf("seed runtime row: %v", err)
	}
	if r, _ := svc.MCPRuntime().Get(m.ID); r == nil || r.ListedAt == 0 {
		t.Fatalf("test setup: expected a seeded runtime row with a non-zero listed_at, got %+v", r)
	}
	if listedAt, _, _ := svc.MCPRuntime().GetSchemas(m.ID); listedAt == 0 {
		t.Fatalf("test setup: expected a seeded schemas row with a non-zero listed_at")
	}

	// Grant a "don't ask again" approval under the OLD identity, through the
	// same internal endpoint Python uses mid-run, so this test proves the
	// gate-level consequence (EffectiveApprovals), not just raw column state.
	runTokens := NewRunTokenStore(time.Minute)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), runTokens, "http://127.0.0.1:1")
	tok := runTokens.Mint("u1", "sess1")
	e := echo.New()
	approvalBody := fmt.Sprintf(`{"server_id":%d,"tool_name":"noop"}`, m.ID)
	approvalReq := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(approvalBody))
	approvalReq.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	approvalReq.Header.Set("X-Agent-MCP-Write-Token", tok)
	approvalRec := httptest.NewRecorder()
	if err := h.ApprovalsInternal(e.NewContext(approvalReq, approvalRec)); err != nil {
		t.Fatalf("ApprovalsInternal: %v", err)
	}
	if approvalRec.Code != http.StatusNoContent {
		t.Fatalf("approvals code = %d, body = %s", approvalRec.Code, approvalRec.Body.String())
	}
	if approvals, err := svc.MCPApprovals().EffectiveApprovals("u1"); err != nil {
		t.Fatalf("EffectiveApprovals (pre-update): %v", err)
	} else if len(approvals) != 1 {
		t.Fatalf("test setup: expected the approval to be effective before the update, got %+v", approvals)
	}

	rec := doUpdate(t, h, m.ID, "u1", `{"url":"https://new.example.com/mcp"}`)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("update code = %d, body = %s", rec.Code, rec.Body.String())
	}

	r, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if r != nil {
		t.Fatalf("expected the runtime row to be deleted after a transport-relevant change, got %+v", r)
	}
	if listedAt, schemasJSON, err := svc.MCPRuntime().GetSchemas(m.ID); err != nil {
		t.Fatalf("GetSchemas: %v", err)
	} else if listedAt != 0 || schemasJSON != "[]" {
		t.Fatalf("expected the schemas row to be deleted too, got listed_at=%d schemas_json=%s", listedAt, schemasJSON)
	}

	approvals, err := svc.MCPApprovals().EffectiveApprovals("u1")
	if err != nil {
		t.Fatalf("EffectiveApprovals (post-update): %v", err)
	}
	if len(approvals) != 0 {
		t.Fatalf("approval granted under the old identity must no longer be effective after a transport-relevant change, got %+v", approvals)
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

// The rename-only test above cannot actually disprove a ciphertext-comparing
// implementation: it sends no env/headers at all, so applyReq never
// re-encrypts those columns and the ciphertext is byte-identical before and
// after by construction — a broken implementation would pass it unchanged.
//
// This is the variant that actually exercises the subtlety: AES-GCM draws a
// fresh random nonce on every Encrypt call (pkg/crypto/masterkey.go), so
// resending the exact same header plaintext on a rename still produces
// different ciphertext bytes. A ciphertext-comparing implementation would
// see "the column changed" and wrongly wipe the identity card; comparing
// decrypted plaintext (what configFPOf actually does) must not.
func TestUpdateDoesNotInvalidateRuntimeOnRenameWhenResendingIdenticalHeaders(t *testing.T) {
	svc := mcpTestSvc(t)
	hdrEnc, err := svc.MasterKey().Encrypt(`{"Authorization":"Bearer secret-value"}`)
	if err != nil {
		t.Fatalf("encrypt headers: %v", err)
	}
	m := &service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://x.example.com/mcp",
		Args: "[]", Env: "{}", Headers: hdrEnc, Enabled: true,
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
	beforeHeaders := m.Headers

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	// Rename AND resend the exact same header content (not a no-op: applyReq
	// re-encrypts whenever req.Headers is non-nil, regardless of whether the
	// plaintext actually differs from what was stored).
	rec := doUpdate(t, h, m.ID, "u1",
		`{"name":"github (renamed)","headers":{"Authorization":"Bearer secret-value"}}`)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("update code = %d, body = %s", rec.Code, rec.Body.String())
	}

	reloaded, err := svc.MCP().GetMcpServer(m.ID, "u1")
	if err != nil {
		t.Fatalf("GetMcpServer: %v", err)
	}
	if reloaded.Headers == beforeHeaders {
		t.Fatal("test setup invalid: headers ciphertext did not change across the update, so this test cannot distinguish plaintext-compare from ciphertext-compare")
	}

	after, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if after == nil {
		t.Fatal("expected the runtime row to still exist")
	}
	if after.ListedAt != before.ListedAt || after.TTLSec != before.TTLSec {
		t.Fatalf("listed_at/ttl_sec changed after a rename that resent identical header plaintext (must compare decrypted content, not ciphertext): before=%+v after=%+v", before, after)
	}
}

// Same subtlety as above, but for env on a stdio server (env is always
// cleared to "{}" for http/sse in validateAndClean, so the env column only
// carries meaningful content on the stdio path).
func TestUpdateDoesNotInvalidateRuntimeOnRenameWhenResendingIdenticalEnv(t *testing.T) {
	svc := mcpTestSvc(t)
	envEnc, err := svc.MasterKey().Encrypt(`{"TOKEN":"secret-value"}`)
	if err != nil {
		t.Fatalf("encrypt env: %v", err)
	}
	m := &service.McpServer{
		UserID: "u1", Name: "local-tool", Transport: "stdio", Command: "npx",
		Args: `["-y","some-server"]`, Env: envEnc, Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{
		ServerID: m.ID, Handle: "local-tool", Summary: "unchanged", TTLSec: 300,
	}, []service.ToolMeta{{Name: "noop"}}, "[]"); err != nil {
		t.Fatalf("seed runtime row: %v", err)
	}
	before, err := svc.MCPRuntime().Get(m.ID)
	if err != nil || before == nil || before.ListedAt == 0 {
		t.Fatalf("test setup: expected a seeded runtime row with a non-zero listed_at, got %+v, err=%v", before, err)
	}
	beforeEnv := m.Env

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	rec := doUpdate(t, h, m.ID, "u1",
		`{"name":"local-tool (renamed)","env":{"TOKEN":"secret-value"}}`)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("update code = %d, body = %s", rec.Code, rec.Body.String())
	}

	reloaded, err := svc.MCP().GetMcpServer(m.ID, "u1")
	if err != nil {
		t.Fatalf("GetMcpServer: %v", err)
	}
	if reloaded.Env == beforeEnv {
		t.Fatal("test setup invalid: env ciphertext did not change across the update, so this test cannot distinguish plaintext-compare from ciphertext-compare")
	}

	after, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if after == nil {
		t.Fatal("expected the runtime row to still exist")
	}
	if after.ListedAt != before.ListedAt || after.TTLSec != before.TTLSec {
		t.Fatalf("listed_at/ttl_sec changed after a rename that resent identical env plaintext (must compare decrypted content, not ciphertext): before=%+v after=%+v", before, after)
	}
}
