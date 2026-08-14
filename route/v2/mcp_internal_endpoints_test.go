package v2

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

// TestApprovalsEndpointRejectsMissingToken pins the 401 contract: the
// authorization subject (which user is granting consent) must never be
// inferable from an unauthenticated call, so a request with no write token
// at all must be rejected before anything else runs.
func TestApprovalsEndpointRejectsMissingToken(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	body := `{"server_id":1,"tool_name":"create_issue"}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	// Deliberately no X-Agent-MCP-Write-Token header.
	rec := httptest.NewRecorder()
	if err := h.ApprovalsInternal(e.NewContext(req, rec)); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for missing write token, got %d: %s", rec.Code, rec.Body.String())
	}
}

// TestApprovalsEndpointRejectsForeignServer pins the ownership check: the
// write token resolves to a user_id, and if that user does not own the
// target server this must 403, or any run could grant approvals on someone
// else's servers.
func TestApprovalsEndpointRejectsForeignServer(t *testing.T) {
	svc := mcpTestSvc(t)
	runTokens := NewRunTokenStore(time.Minute)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), runTokens, "http://127.0.0.1:1")
	e := echo.New()

	// Server belongs to "owner", not to "attacker".
	m := &service.McpServer{
		UserID: "owner", Name: "github", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}

	tok := runTokens.Mint("attacker", "sess1")
	body := fmt.Sprintf(`{"server_id":%d,"tool_name":"create_issue"}`, m.ID)
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-Agent-MCP-Write-Token", tok)
	rec := httptest.NewRecorder()
	err := h.ApprovalsInternal(e.NewContext(req, rec))
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for foreign server, got %v (code %d)", err, rec.Code)
	}

	// No approval must have been written for the real owner as a side effect
	// of the rejected attempt.
	rows, err := svc.MCPApprovals().ListForServer(m.ID)
	if err != nil {
		t.Fatalf("ListForServer: %v", err)
	}
	if len(rows) != 0 {
		t.Fatalf("approval must not be written when ownership check fails: %+v", rows)
	}
}

// TestApprovalsEndpointStampsCurrentFingerprintAndHash pins the core
// security property of this endpoint: identity_fp and schema_hash are always
// read from the server's CURRENT mcp_server_runtime row, never from the
// request body. If the caller could supply them directly, it could forge an
// approval that compares equal to itself forever, defeating the config and
// interface gates entirely.
func TestApprovalsEndpointStampsCurrentFingerprintAndHash(t *testing.T) {
	svc := mcpTestSvc(t)
	runTokens := NewRunTokenStore(time.Minute)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), runTokens, "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	tools := []service.ToolMeta{{Name: "create_issue", SchemaHash: "real-hash", DescHash: "d"}}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "real-fp", TTLSec: 3600},
		tools, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	tok := runTokens.Mint("u1", "sess1")
	// Try to smuggle forged identity_fp/schema_hash in the body alongside the
	// legitimate fields; the handler must ignore both extra fields entirely.
	body := fmt.Sprintf(`{"server_id":%d,"tool_name":"create_issue","identity_fp":"forged-fp","schema_hash":"forged-hash"}`, m.ID)
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-Agent-MCP-Write-Token", tok)
	rec := httptest.NewRecorder()
	if err := h.ApprovalsInternal(e.NewContext(req, rec)); err != nil {
		t.Fatalf("approvals: %v", err)
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", rec.Code, rec.Body.String())
	}

	// If the handler had stored the forged values, this would find zero rows:
	// EffectiveApprovals compares the stored identity_fp against the CURRENT
	// runtime identity_fp ("real-fp"), and "forged-fp" would never match it.
	approvals, err := svc.MCPApprovals().EffectiveApprovals("u1")
	if err != nil {
		t.Fatalf("EffectiveApprovals: %v", err)
	}
	if len(approvals) != 1 || approvals[0].ToolName != "create_issue" {
		t.Fatalf("expected one effective approval stamped from the runtime row, got %+v", approvals)
	}

	// Extra confirmation: if the server's identity later changes for real,
	// the approval this call just wrote must void — proving it was stamped
	// with "real-fp" (a value the runtime can legitimately move away from),
	// not "forged-fp" (a value nothing else in the system would ever
	// legitimately produce again to compare against).
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "changed-fp", TTLSec: 3600},
		tools, "[]"); err != nil {
		t.Fatalf("update runtime: %v", err)
	}
	approvals2, err := svc.MCPApprovals().EffectiveApprovals("u1")
	if err != nil {
		t.Fatalf("EffectiveApprovals after identity change: %v", err)
	}
	if len(approvals2) != 0 {
		t.Fatalf("approval must void once the server's real identity_fp changes, got %+v", approvals2)
	}
}

// TestSchemasEndpointReturnsListedAtForCacheKeying pins the response shape:
// listed_at must ship alongside the schema bodies. Python's in-memory cache
// is keyed on listed_at; without it a changed tool description can never
// invalidate the cache and reach the model.
func TestSchemasEndpointReturnsListedAtForCacheKeying(t *testing.T) {
	svc := mcpTestSvc(t)
	runTokens := NewRunTokenStore(time.Minute)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), runTokens, "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	schemasJSON := `[{"name":"create_issue","description":"d","input_schema":{}}]`
	tools := []service.ToolMeta{{Name: "create_issue", SchemaHash: "h", DescHash: "d"}}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, TTLSec: 3600}, tools, schemasJSON); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	wantListedAt, _, err := svc.MCPRuntime().GetSchemas(m.ID)
	if err != nil {
		t.Fatalf("GetSchemas: %v", err)
	}
	if wantListedAt == 0 {
		t.Fatalf("test setup did not produce a nonzero listed_at")
	}

	tok := runTokens.Mint("u1", "sess1")
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Agent-MCP-Write-Token", tok)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues(fmt.Sprint(m.ID))
	if err := h.SchemasInternal(c); err != nil {
		t.Fatalf("SchemasInternal: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	var out struct {
		ListedAt int64            `json:"listed_at"`
		Schemas  []map[string]any `json:"schemas"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if out.ListedAt != wantListedAt {
		t.Fatalf("expected listed_at=%d for cache keying, got %d", wantListedAt, out.ListedAt)
	}
	if len(out.Schemas) != 1 || out.Schemas[0]["name"] != "create_issue" {
		t.Fatalf("expected the schema body to pass through, got %+v", out.Schemas)
	}
}

// TestApprovalsEndpointRejectsInvalidOrExpiredToken extends the
// missing-token coverage to the two other ways a token can fail to resolve:
// a token string that was never minted at all, and one that WAS minted but
// has since expired. Both must 401, exactly like a missing token.
func TestApprovalsEndpointRejectsInvalidOrExpiredToken(t *testing.T) {
	svc := mcpTestSvc(t)
	body := `{"server_id":1,"tool_name":"create_issue"}`

	t.Run("unknown token", func(t *testing.T) {
		h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
		e := echo.New()
		req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
		req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
		req.Header.Set("X-Agent-MCP-Write-Token", "not-a-real-token")
		rec := httptest.NewRecorder()
		if err := h.ApprovalsInternal(e.NewContext(req, rec)); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("expected 401 for unknown token, got %d: %s", rec.Code, rec.Body.String())
		}
	})

	t.Run("expired token", func(t *testing.T) {
		// A negative TTL makes Mint hand back a token whose expiry is
		// already in the past, without needing to sleep in the test.
		expiredTokens := NewRunTokenStore(-time.Minute)
		h := NewMCPHandler(svc, NewTicketStore(time.Minute), expiredTokens, "http://127.0.0.1:1")
		e := echo.New()
		tok := expiredTokens.Mint("u1", "sess1")
		req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
		req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
		req.Header.Set("X-Agent-MCP-Write-Token", tok)
		rec := httptest.NewRecorder()
		if err := h.ApprovalsInternal(e.NewContext(req, rec)); err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("expected 401 for expired token, got %d: %s", rec.Code, rec.Body.String())
		}
	})
}

// TestApprovalsEndpointRoutesWildcardToServerLevel pins the "*" →
// PutServerLevel routing decision directly: if this ever regressed into
// calling Put("*", ...) instead, Task 10's Put rejects tool_name=="*" and
// this request would 500 instead of succeeding.
func TestApprovalsEndpointRoutesWildcardToServerLevel(t *testing.T) {
	svc := mcpTestSvc(t)
	runTokens := NewRunTokenStore(time.Minute)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), runTokens, "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "t", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	tok := runTokens.Mint("u1", "sess1")
	body := fmt.Sprintf(`{"server_id":%d,"tool_name":"*"}`, m.ID)
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-Agent-MCP-Write-Token", tok)
	rec := httptest.NewRecorder()
	if err := h.ApprovalsInternal(e.NewContext(req, rec)); err != nil {
		t.Fatalf("wildcard approval must route to PutServerLevel and succeed, got error: %v", err)
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", rec.Code, rec.Body.String())
	}

	rows, err := svc.MCPApprovals().ListForServer(m.ID)
	if err != nil {
		t.Fatalf("ListForServer: %v", err)
	}
	found := false
	for _, r := range rows {
		if r.ToolName == "*" {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected a server-level '*' approval row, got %+v", rows)
	}
}

// TestApprovalsEndpointRejectsNonexistentServerLikeForeignServer pins the
// no-enumeration-oracle property: a server_id that doesn't exist at all must
// produce the exact same 403 as a server_id that exists but belongs to
// someone else, never a distinct 404 that would let a caller tell the two
// apart.
func TestApprovalsEndpointRejectsNonexistentServerLikeForeignServer(t *testing.T) {
	svc := mcpTestSvc(t)
	runTokens := NewRunTokenStore(time.Minute)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), runTokens, "http://127.0.0.1:1")
	e := echo.New()

	tok := runTokens.Mint("u1", "sess1")
	body := `{"server_id":999999,"tool_name":"create_issue"}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-Agent-MCP-Write-Token", tok)
	rec := httptest.NewRecorder()
	err := h.ApprovalsInternal(e.NewContext(req, rec))
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for a nonexistent server_id (same as a foreign one, no enumeration oracle), got %v", err)
	}
}

// TestSchemasEndpointRejectsMissingToken pins that SchemasInternal is NOT
// bare LocalhostOnly plumbing: it returns user-scoped tool schemas keyed by
// a sequential integer id, so it needs the same positive credential as
// ApprovalsInternal, not just the internal group's network gate.
func TestSchemasEndpointRejectsMissingToken(t *testing.T) {
	h := NewMCPHandler(mcpTestSvc(t), NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues("1")
	if err := h.SchemasInternal(c); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for missing write token, got %d: %s", rec.Code, rec.Body.String())
	}
}

// TestSchemasEndpointRejectsForeignServer pins the ownership check on the
// read path: a valid write token for one user must not be usable to read
// another user's server schemas — without this, any run's write token would
// double as a cross-user read credential over every server's tool argument
// schemas and descriptions.
func TestSchemasEndpointRejectsForeignServer(t *testing.T) {
	svc := mcpTestSvc(t)
	runTokens := NewRunTokenStore(time.Minute)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), runTokens, "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{
		UserID: "owner", Name: "github", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}

	tok := runTokens.Mint("attacker", "sess1")
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Agent-MCP-Write-Token", tok)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues(fmt.Sprint(m.ID))
	err := h.SchemasInternal(c)
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for a token whose user does not own the server, got %v", err)
	}
}
