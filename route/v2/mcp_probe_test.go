package v2

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

func TestBuildHandlePrefersServerInfoName(t *testing.T) {
	got := BuildHandle(map[string]string{"name": "github-mcp-server"},
		"http", "https://x/mcp", "", nil, "测试1")
	if got != "github" {
		t.Fatalf("handle = %q, want %q — noise words mcp/server must be stripped", got, "github")
	}
}

func TestBuildHandleFallsBackToNpmPackage(t *testing.T) {
	got := BuildHandle(nil, "stdio", "", "npx", []string{"-y", "@modelcontextprotocol/server-github"}, "测试1")
	if got != "github" {
		t.Fatalf("handle = %q, want github", got)
	}
}

func TestBuildHandleFallsBackToURLHost(t *testing.T) {
	if got := BuildHandle(nil, "http", "https://mcp.notion.com/mcp", "", nil, "测试1"); got != "notion" {
		t.Fatalf("handle = %q, want notion", got)
	}
}

func TestBuildHandleLastResortIsUserName(t *testing.T) {
	// Only fall back to the user-typed name once every automatic signal is unavailable.
	if got := BuildHandle(nil, "stdio", "", "uvx", nil, "My Server"); got != "my_server" {
		t.Fatalf("handle = %q, want my_server", got)
	}
}

func TestBuildSummaryPrefersInstructions(t *testing.T) {
	s := BuildSummary("Tools for GitHub. More detail follows and is not needed here.",
		nil, "http", "https://x", "", nil, nil)
	if s != "Tools for GitHub." {
		t.Fatalf("summary = %q, want the first sentence of instructions", s)
	}
}

func TestBuildSummaryFallsBackThroughChain(t *testing.T) {
	// With no instructions / serverInfo, fall back to the connection target —
	// this link in the chain costs zero network calls and is always available.
	s := BuildSummary("", nil, "http", "https://mcp.notion.com/mcp", "", nil, nil)
	if s == "" {
		t.Fatal("summary must never be empty: the connection target is always available")
	}
}

// Review finding 3: a stdio server whose user-typed name slugifies to "" (no
// args, no url either) must fall through to the command basename before
// BuildHandle gives up and returns "".
func TestBuildHandleFallsBackToCommandWhenNameUnusable(t *testing.T) {
	got := BuildHandle(nil, "stdio", "", "python3", nil, "测试")
	if got != "python3" {
		t.Fatalf("handle = %q, want python3 (the command basename)", got)
	}
}

// Review finding 3: when even the command is empty/noise-only, BuildHandle
// legitimately returns "" — it stays pure and never invents an id-based
// fallback itself.
func TestBuildHandleReturnsEmptyWhenEveryAutomaticSignalIsUnusable(t *testing.T) {
	got := BuildHandle(nil, "stdio", "", "", nil, "测试")
	if got != "" {
		t.Fatalf("handle = %q, want \"\" — the caller, not BuildHandle, must supply the synthetic fallback", got)
	}
}

// Review finding 4a: table test for protocolModeFor.
func TestProtocolModeFor(t *testing.T) {
	cases := []struct {
		name, era, version, want string
	}{
		{"modern with version pins the exact version", "modern", "2025-06-18", "2025-06-18"},
		{"modern with empty version falls back to auto", "modern", "", "auto"},
		{"legacy pins legacy", "legacy", "2024-11-05", "legacy"},
		{"legacy with empty version still pins legacy", "legacy", "", "legacy"},
		{"missing era falls back to auto", "", "2025-06-18", "auto"},
		{"unknown era falls back to auto", "unknown", "", "auto"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := protocolModeFor(tc.era, tc.version); got != tc.want {
				t.Fatalf("protocolModeFor(%q, %q) = %q, want %q", tc.era, tc.version, got, tc.want)
			}
		})
	}
}

// probeAgentStub returns an httptest.Server that always answers
// /agent/mcp/test with the given JSON body, and lets the test capture the
// request the handler sent it.
func probeAgentStub(t *testing.T, respBody string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(respBody))
	}))
}

func testEcho(method, id, uid string) (echo.Context, *httptest.ResponseRecorder) {
	e := echo.New()
	req := httptest.NewRequest(method, "/", nil)
	if uid != "" {
		req.Header.Set("X-NimoOS-User-ID", uid)
	}
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues(id)
	return c, rec
}

// Review finding 4b: a full success round trip must persist the identity
// card — handle, summary, fingerprints, protocol mode and the tool list —
// not just proxy the response back to the browser.
func TestTest_SuccessPersistsRuntimeRow(t *testing.T) {
	svc := mcpTestSvc(t)
	enc, _ := svc.MasterKey().Encrypt(`{"Authorization":"Bearer S"}`)
	m := &service.McpServer{
		UserID: "u1", Name: "测试1", Transport: "http", URL: "https://x/mcp",
		Args: "[]", Env: "{}", Headers: enc, Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}

	agent := probeAgentStub(t, `{
		"ok": true, "tool_count": 1, "tools": ["search_repos"],
		"protocol_era": "modern", "protocol_version": "2025-06-18",
		"supported_versions": ["2025-06-18"],
		"instructions": "Tools for GitHub. More detail follows and is not needed here.",
		"server_info": {"name": "github-mcp-server", "title": "GitHub MCP", "version": "1.2.3", "description": ""},
		"ttl_sec": 300,
		"tool_metas": [{"name": "search_repos", "schema_hash": "sh1", "desc_hash": "dh1"}],
		"schemas": [{"name": "search_repos", "description": "Search repositories.", "input_schema": {"type": "object"}}]
	}`)
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)
	c, rec := testEcho(http.MethodPost, fmt.Sprintf("%d", m.ID), "u1")
	if err := h.Test(c); err != nil {
		t.Fatalf("Test: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("Test code = %d, body = %s", rec.Code, rec.Body.String())
	}

	r, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if r == nil {
		t.Fatal("expected a persisted runtime row after a successful probe")
	}
	if r.ProbeState != "ok" {
		t.Fatalf("probe_state = %q, want ok", r.ProbeState)
	}
	if r.Handle != "github" {
		t.Fatalf("handle = %q, want github (from server_info.name, not the user-typed 测试1)", r.Handle)
	}
	if r.Summary != "Tools for GitHub." {
		t.Fatalf("summary = %q, want the first sentence of instructions", r.Summary)
	}
	if r.ServerName != "github-mcp-server" || r.ServerTitle != "GitHub MCP" || r.ServerVersion != "1.2.3" {
		t.Fatalf("server identity not persisted verbatim: %+v", r)
	}
	if r.ProtocolEra != "modern" {
		t.Fatalf("protocol_era = %q, want modern", r.ProtocolEra)
	}
	if r.ProtocolMode != "2025-06-18" {
		t.Fatalf("protocol_mode = %q, want the pinned negotiated version 2025-06-18", r.ProtocolMode)
	}
	wantCfgFP := service.ConfigFP("http", "https://x/mcp", "", []string{},
		map[string]string{}, map[string]string{"Authorization": "Bearer S"})
	wantIDFP := service.IdentityFP("http", "https://x/mcp", "", []string{},
		map[string]string{}, map[string]string{"Authorization": "Bearer S"})
	if r.ConfigFP != wantCfgFP {
		t.Fatalf("config_fp = %q, want %q", r.ConfigFP, wantCfgFP)
	}
	if r.IdentityFP != wantIDFP {
		t.Fatalf("identity_fp = %q, want %q", r.IdentityFP, wantIDFP)
	}
	var tools []service.ToolMeta
	if err := json.Unmarshal([]byte(r.ToolsJSON), &tools); err != nil {
		t.Fatalf("unmarshal tools_json: %v body=%s", err, r.ToolsJSON)
	}
	if len(tools) != 1 || tools[0].Name != "search_repos" || tools[0].SchemaHash != "sh1" || tools[0].DescHash != "dh1" {
		t.Fatalf("tools not persisted verbatim: %+v", tools)
	}
}

// Review finding 4c: a probe that comes back ok:false must persist via
// SaveFailure and must NOT leave probe_state stuck at 'probing'.
func TestTest_FailurePersistsAndClearsLock(t *testing.T) {
	svc := mcpTestSvc(t)
	m := &service.McpServer{
		UserID: "u1", Name: "flaky", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}

	agent := probeAgentStub(t, `{"ok": false, "error": "Connection failed: boom", "error_key": "connect_failed"}`)
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)
	c, rec := testEcho(http.MethodPost, fmt.Sprintf("%d", m.ID), "u1")
	if err := h.Test(c); err != nil {
		t.Fatalf("Test: %v", err)
	}
	var respBody map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &respBody); err != nil {
		t.Fatalf("unmarshal response: %v body=%s", err, rec.Body.String())
	}
	if rec.Code != http.StatusOK || respBody["ok"] != false {
		t.Fatalf("expected the failed probe result to reach the browser, got %d %s", rec.Code, rec.Body.String())
	}

	r, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if r == nil {
		t.Fatal("expected SaveFailure to have created a runtime row")
	}
	if r.ProbeState == "probing" {
		t.Fatal("probe_state left at 'probing' — the single-flight lock is wedged")
	}
	if r.ProbeState != "failed" {
		t.Fatalf("probe_state = %q, want failed", r.ProbeState)
	}
	if r.LastError != "Connection failed: boom" || r.LastErrorKey != "connect_failed" {
		t.Fatalf("failure not persisted verbatim: last_error=%q last_error_key=%q", r.LastError, r.LastErrorKey)
	}
	if r.FailStreak != 1 {
		t.Fatalf("fail_streak = %d, want 1", r.FailStreak)
	}
}

// Review finding 3 (caller side): when BuildHandle itself has nothing to
// work with — no server_info, no args, no url, a command that's empty, and a
// user-typed name that slugifies to "" — probeAndPersist must still persist
// a non-empty, unique handle rather than storing "".
func TestTest_SyntheticHandleWhenEverySignalIsUnusable(t *testing.T) {
	svc := mcpTestSvc(t)
	m := &service.McpServer{
		UserID: "u1", Name: "测试", Transport: "stdio", Command: "",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}

	agent := probeAgentStub(t, `{
		"ok": true, "tool_count": 1, "tools": ["noop"],
		"tool_metas": [{"name": "noop", "schema_hash": "h", "desc_hash": "d"}]
	}`)
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)
	c, rec := testEcho(http.MethodPost, fmt.Sprintf("%d", m.ID), "u1")
	if err := h.Test(c); err != nil {
		t.Fatalf("Test: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("Test code = %d, body = %s", rec.Code, rec.Body.String())
	}

	r, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("MCPRuntime().Get: %v", err)
	}
	if r == nil {
		t.Fatal("expected a persisted runtime row")
	}
	want := fmt.Sprintf("server_%d", m.ID)
	if r.Handle != want {
		t.Fatalf("handle = %q, want synthetic fallback %q", r.Handle, want)
	}
}
