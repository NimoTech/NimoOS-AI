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

// setParams is a small helper for setting echo path params in tests below.
func setParams(c echo.Context, names, values []string) {
	c.SetParamNames(names...)
	c.SetParamValues(values...)
}

// TestToolsEndpointReturnsToolsWithApprovalStateAndLastSeenAt pins Step 3
// assertion 1: GET .../tools reads tools_json + mcp_tool_approvals only (no
// agentURL is ever dialed — this test's handler is built with an
// unreachable agentURL to prove it) and reports each tool's approval state
// and last_seen_at.
func TestToolsEndpointReturnsToolsWithApprovalStateAndLastSeenAt(t *testing.T) {
	svc := mcpTestSvc(t)
	// Deliberately unreachable: proves Tools() never dials out.
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{
			{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh1"},
			{Name: "close_issue", SchemaHash: "sh2", DescHash: "dh2"},
		}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	if err := svc.MCPApprovals().Put(m.ID, "create_issue", "fp", "sh1", "dh1"); err != nil {
		t.Fatalf("approve: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	var out struct {
		Tools []struct {
			Name        string `json:"name"`
			Approved    bool   `json:"approved"`
			LastSeenAt  int64  `json:"last_seen_at"`
			DescChanged bool   `json:"desc_changed"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if len(out.Tools) != 2 {
		t.Fatalf("expected 2 tools, got %+v", out.Tools)
	}
	byName := map[string]struct {
		Approved    bool
		LastSeenAt  int64
		DescChanged bool
	}{}
	for _, tl := range out.Tools {
		byName[tl.Name] = struct {
			Approved    bool
			LastSeenAt  int64
			DescChanged bool
		}{tl.Approved, tl.LastSeenAt, tl.DescChanged}
	}
	ci, ok := byName["create_issue"]
	if !ok || !ci.Approved || ci.LastSeenAt == 0 {
		t.Fatalf("expected create_issue approved with nonzero last_seen_at, got %+v", byName)
	}
	cl, ok := byName["close_issue"]
	if !ok || cl.Approved || cl.LastSeenAt != 0 {
		t.Fatalf("expected close_issue unapproved with zero last_seen_at, got %+v", byName)
	}
}

// TestToolsEndpointTotalStoredApprovalsCountsRemovedToolApprovals pins the
// mcp-progressive-disclosure Task 21 fix round: total_stored_approvals must
// count EVERY row ListForServer returns for this server, including an
// approval whose tool has since disappeared from the live tools_json
// snapshot -- that approval gets no toolStateDTO row at all (the Tools loop
// only ranges over metas), so len(tools) alone would silently undercount
// exactly what CASCADE will delete if the server is removed.
func TestToolsEndpointTotalStoredApprovalsCountsRemovedToolApprovals(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	// First handshake still lists "removed_tool" -- approve it while it's
	// live, exactly like a real user would.
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{
			{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh1"},
			{Name: "removed_tool", SchemaHash: "sh2", DescHash: "dh2"},
		}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	if err := svc.MCPApprovals().Put(m.ID, "create_issue", "fp", "sh1", "dh1"); err != nil {
		t.Fatalf("approve create_issue: %v", err)
	}
	if err := svc.MCPApprovals().Put(m.ID, "removed_tool", "fp", "sh2", "dh2"); err != nil {
		t.Fatalf("approve removed_tool: %v", err)
	}
	// A later re-probe no longer lists "removed_tool" -- its approval row is
	// untouched in mcp_tool_approvals (nothing here deletes it), but the
	// Tools loop below only ranges over the CURRENT tools_json, so it will
	// never produce a toolStateDTO for it.
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{
			{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh1"},
		}, "[]"); err != nil {
		t.Fatalf("re-probe runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	var out struct {
		Tools                []struct{ Name string } `json:"tools"`
		TotalStoredApprovals int                      `json:"total_stored_approvals"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if len(out.Tools) != 1 {
		t.Fatalf("expected 1 tool row (removed_tool dropped from the live snapshot), got %+v", out.Tools)
	}
	if out.TotalStoredApprovals != 2 {
		t.Fatalf("expected total_stored_approvals=2 (both stored rows, including the removed tool's), got %d; body=%s",
			out.TotalStoredApprovals, rec.Body.String())
	}
	if out.TotalStoredApprovals <= len(out.Tools) {
		t.Fatalf("total_stored_approvals (%d) must exceed the returned tool row count (%d) in this scenario -- "+
			"otherwise the removed tool's approval is silently uncounted", out.TotalStoredApprovals, len(out.Tools))
	}
}

// TestToolsEndpointSetsDescChanged pins Step 3 assertion 2: desc_changed is
// true when the stored desc_hash differs from the current one, and false
// when the stored value is empty (the pre-upgrade case) — never true just
// because it is empty.
func TestToolsEndpointSetsDescChanged(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{
			{Name: "changed", SchemaHash: "sh", DescHash: "new-desc"},
			{Name: "upgraded", SchemaHash: "sh", DescHash: "some-desc"},
		}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	// "changed": approved when the description hash was "old-desc"; the probe
	// above already moved tools_json's desc_hash to "new-desc".
	if err := svc.MCPApprovals().Put(m.ID, "changed", "fp", "sh", "old-desc"); err != nil {
		t.Fatalf("approve changed: %v", err)
	}
	// "upgraded": approved before desc_hash existed — stored value is "".
	if err := svc.MCPApprovals().Put(m.ID, "upgraded", "fp", "sh", ""); err != nil {
		t.Fatalf("approve upgraded: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}

	var out struct {
		Tools []struct {
			Name        string `json:"name"`
			DescChanged bool   `json:"desc_changed"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	for _, tl := range out.Tools {
		switch tl.Name {
		case "changed":
			if !tl.DescChanged {
				t.Fatalf("expected desc_changed=true for 'changed', got %+v", tl)
			}
		case "upgraded":
			if tl.DescChanged {
				t.Fatalf("expected desc_changed=false for pre-upgrade empty stored hash, got %+v", tl)
			}
		default:
			t.Fatalf("unexpected tool %q", tl.Name)
		}
	}
}

// TestToolsEndpointNoRuntimeRowReturnsEmptyList pins that a server which has
// never been probed is a normal state: empty tool list, not an error.
func TestToolsEndpointNoRuntimeRowReturnsEmptyList(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	// Deliberately no SaveSuccess: no runtime row at all.

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 (empty list, not an error), got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"tools":[]`) {
		t.Fatalf(`expected {"tools":[]}, got %s`, rec.Body.String())
	}
}

// TestToolsEndpointRejectsForeignAndNonexistentServer pins Step 3 assertion
// 7 for this endpoint: a foreign server and a nonexistent one both 403.
func TestToolsEndpointRejectsForeignAndNonexistentServer(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "owner", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}

	for _, tc := range []struct {
		name string
		id   string
	}{
		{"foreign server", fmt.Sprint(m.ID)},
		{"nonexistent server", "999999"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/", nil)
			req.Header.Set("X-NimoOS-User-ID", "attacker")
			rec := httptest.NewRecorder()
			c := e.NewContext(req, rec)
			setParams(c, []string{"id"}, []string{tc.id})
			err := h.Tools(c)
			he, ok := err.(*echo.HTTPError)
			if !ok || he.Code != http.StatusForbidden {
				t.Fatalf("expected 403, got %v", err)
			}
		})
	}
}

// TestPutApprovalStampsFromRuntimeRowIgnoringBody pins Step 3 assertion 3:
// identity_fp/schema_hash/desc_hash are stamped from the CURRENT runtime row,
// and any of those fields supplied in the request body are ignored entirely.
func TestPutApprovalStampsFromRuntimeRowIgnoringBody(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "real-fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "real-sh", DescHash: "real-dh"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	body := `{"approved":true,"identity_fp":"forged-fp","schema_hash":"forged-sh","desc_hash":"forged-dh"}`
	req := httptest.NewRequest(http.MethodPut, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id", "tool"}, []string{fmt.Sprint(m.ID), "create_issue"})
	if err := h.PutApproval(c); err != nil {
		t.Fatalf("PutApproval: %v", err)
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", rec.Code, rec.Body.String())
	}

	rows, err := svc.MCPApprovals().ListForServer(m.ID)
	if err != nil {
		t.Fatalf("ListForServer: %v", err)
	}
	if len(rows) != 1 || rows[0].DescHash != "real-dh" {
		t.Fatalf("expected stored desc_hash from the runtime row, got %+v", rows)
	}

	// Prove the forged identity_fp was never stored: EffectiveApprovals
	// compares the stored value against the CURRENT runtime identity_fp
	// ("real-fp"); a stored "forged-fp" would never match it either, but a
	// self-consistent forgery would — this only proves the write succeeded
	// with the real value by checking it directly below instead.
	var storedFP, storedSchema string
	if err := svc.DB().QueryRow(
		`SELECT identity_fp, schema_hash FROM mcp_tool_approvals WHERE server_id=? AND tool_name='create_issue'`,
		m.ID).Scan(&storedFP, &storedSchema); err != nil {
		t.Fatalf("select: %v", err)
	}
	if storedFP != "real-fp" || storedSchema != "real-sh" {
		t.Fatalf("expected identity_fp/schema_hash stamped from the runtime row, got fp=%q schema=%q", storedFP, storedSchema)
	}
}

// TestPutApprovalWildcardGrantsAndRevokesServerLevel pins Step 3 assertion 4:
// ":tool"=="*" routes to PutServerLevel on grant and to Delete(serverID,"*")
// on revoke.
func TestPutApprovalWildcardGrantsAndRevokesServerLevel(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "t", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	grant := httptest.NewRequest(http.MethodPut, "/", strings.NewReader(`{"approved":true}`))
	grant.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	grant.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(grant, rec)
	setParams(c, []string{"id", "tool"}, []string{fmt.Sprint(m.ID), "*"})
	if err := h.PutApproval(c); err != nil {
		t.Fatalf("grant: %v", err)
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204 on grant, got %d: %s", rec.Code, rec.Body.String())
	}
	rows, _ := svc.MCPApprovals().ListForServer(m.ID)
	found := false
	for _, r := range rows {
		if r.ToolName == "*" {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected server-level '*' approval after grant, got %+v", rows)
	}

	revoke := httptest.NewRequest(http.MethodPut, "/", strings.NewReader(`{"approved":false}`))
	revoke.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	revoke.Header.Set("X-NimoOS-User-ID", "u1")
	rec2 := httptest.NewRecorder()
	c2 := e.NewContext(revoke, rec2)
	setParams(c2, []string{"id", "tool"}, []string{fmt.Sprint(m.ID), "*"})
	if err := h.PutApproval(c2); err != nil {
		t.Fatalf("revoke: %v", err)
	}
	rows2, _ := svc.MCPApprovals().ListForServer(m.ID)
	for _, r := range rows2 {
		if r.ToolName == "*" {
			t.Fatalf("expected '*' approval removed after revoke, got %+v", rows2)
		}
	}
}

// TestDeleteApprovalsRemovesOnlyThatServer pins Step 3 assertion 5.
func TestDeleteApprovalsRemovesOnlyThatServer(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m1 := &service.McpServer{UserID: "u1", Name: "s1", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	m2 := &service.McpServer{UserID: "u1", Name: "s2", Transport: "http", URL: "https://y", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m1); err != nil {
		t.Fatalf("create m1: %v", err)
	}
	if err := svc.MCP().CreateMcpServer(m2); err != nil {
		t.Fatalf("create m2: %v", err)
	}
	if err := svc.MCPApprovals().Put(m1.ID, "t", "fp", "sh", ""); err != nil {
		t.Fatalf("approve m1: %v", err)
	}
	if err := svc.MCPApprovals().Put(m2.ID, "t", "fp", "sh", ""); err != nil {
		t.Fatalf("approve m2: %v", err)
	}

	req := httptest.NewRequest(http.MethodDelete, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m1.ID)})
	if err := h.DeleteApprovals(c); err != nil {
		t.Fatalf("DeleteApprovals: %v", err)
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", rec.Code, rec.Body.String())
	}

	rows1, _ := svc.MCPApprovals().ListForServer(m1.ID)
	if len(rows1) != 0 {
		t.Fatalf("expected all approvals removed for m1, got %+v", rows1)
	}
	rows2, _ := svc.MCPApprovals().ListForServer(m2.ID)
	if len(rows2) != 1 {
		t.Fatalf("expected m2's approval untouched, got %+v", rows2)
	}
}

// TestDeleteApprovalsRejectsForeignAndNonexistentServer pins Step 3 assertion
// 7 for this endpoint.
func TestDeleteApprovalsRejectsForeignAndNonexistentServer(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	m := &service.McpServer{UserID: "owner", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}

	for _, id := range []string{fmt.Sprint(m.ID), "999999"} {
		req := httptest.NewRequest(http.MethodDelete, "/", nil)
		req.Header.Set("X-NimoOS-User-ID", "attacker")
		rec := httptest.NewRecorder()
		c := e.NewContext(req, rec)
		setParams(c, []string{"id"}, []string{id})
		err := h.DeleteApprovals(c)
		he, ok := err.(*echo.HTTPError)
		if !ok || he.Code != http.StatusForbidden {
			t.Fatalf("expected 403 for id=%s, got %v", id, err)
		}
	}
}

// TestPutApprovalRejectsForeignAndNonexistentServer pins Step 3 assertion 7
// for the PUT endpoint.
func TestPutApprovalRejectsForeignAndNonexistentServer(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	m := &service.McpServer{UserID: "owner", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}

	for _, id := range []string{fmt.Sprint(m.ID), "999999"} {
		body := `{"approved":true}`
		req := httptest.NewRequest(http.MethodPut, "/", strings.NewReader(body))
		req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
		req.Header.Set("X-NimoOS-User-ID", "attacker")
		rec := httptest.NewRecorder()
		c := e.NewContext(req, rec)
		setParams(c, []string{"id", "tool"}, []string{id, "create_issue"})
		err := h.PutApproval(c)
		he, ok := err.(*echo.HTTPError)
		if !ok || he.Code != http.StatusForbidden {
			t.Fatalf("expected 403 for id=%s, got %v", id, err)
		}
	}
}

// TestListApprovalsReturnsOnlyCallersApprovalsWithServerHandle pins Step 3
// assertion 6: GET /mcp/approvals returns only the caller's approvals, each
// carrying server_handle.
func TestListApprovalsReturnsOnlyCallersApprovalsWithServerHandle(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	mine := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	theirs := &service.McpServer{UserID: "u2", Name: "slack", Transport: "http", URL: "https://y", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(mine); err != nil {
		t.Fatalf("create mine: %v", err)
	}
	if err := svc.MCP().CreateMcpServer(theirs); err != nil {
		t.Fatalf("create theirs: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: mine.ID, Handle: "github", IdentityFP: "fp1", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("seed mine runtime: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: theirs.ID, Handle: "slack", IdentityFP: "fp2", TTLSec: 3600},
		[]service.ToolMeta{{Name: "post_message", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("seed theirs runtime: %v", err)
	}
	if err := svc.MCPApprovals().Put(mine.ID, "create_issue", "fp1", "sh", ""); err != nil {
		t.Fatalf("approve mine: %v", err)
	}
	if err := svc.MCPApprovals().Put(theirs.ID, "post_message", "fp2", "sh", ""); err != nil {
		t.Fatalf("approve theirs: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	if err := h.ListApprovals(e.NewContext(req, rec)); err != nil {
		t.Fatalf("ListApprovals: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	var out struct {
		Items []struct {
			ServerID     int64  `json:"server_id"`
			ServerHandle string `json:"server_handle"`
			ToolName     string `json:"tool_name"`
		} `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if len(out.Items) != 1 {
		t.Fatalf("expected only the caller's own approval, got %+v", out.Items)
	}
	if out.Items[0].ServerID != mine.ID || out.Items[0].ToolName != "create_issue" {
		t.Fatalf("unexpected item: %+v", out.Items[0])
	}
	if out.Items[0].ServerHandle != "github" {
		t.Fatalf("expected server_handle %q, got %q", "github", out.Items[0].ServerHandle)
	}
}

// TestToolsEndpointSurfacesStaleReasonForConfigVoidedApproval pins that a
// currently-void approval is NOT reported as a plain, unqualified "approved"
// toggle: the config gate (server identity changed — e.g. the user edited
// the URL) is not derivable client-side, since the browser never sees
// identity_fp. Without stale_reason surviving into the response, the tools
// page would show every toggle still on, telling the user they will not be
// re-prompted when in fact they will be.
func TestToolsEndpointSurfacesStaleReasonForConfigVoidedApproval(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "old-fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	if err := svc.MCPApprovals().Put(m.ID, "create_issue", "old-fp", "sh", ""); err != nil {
		t.Fatalf("approve: %v", err)
	}
	// Simulate the user editing the server's URL: identity_fp moves, voiding
	// the approval on the config gate.
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "new-fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("update runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}

	var out struct {
		Tools []struct {
			Name        string `json:"name"`
			StaleReason string `json:"stale_reason"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if len(out.Tools) != 1 || out.Tools[0].StaleReason == "" {
		t.Fatalf("expected a non-empty stale_reason for the config-voided approval, got %+v", out.Tools)
	}
}

// TestToolsEndpointSurfacesStaleReasonKeyAlongsideProse pins that
// stale_reason_key (the machine-readable counterpart added so the UI can map
// through its own i18n table instead of rendering English prose directly)
// rides alongside stale_reason rather than replacing it, using the same
// config-voided setup as the test above.
func TestToolsEndpointSurfacesStaleReasonKeyAlongsideProse(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "old-fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	if err := svc.MCPApprovals().Put(m.ID, "create_issue", "old-fp", "sh", ""); err != nil {
		t.Fatalf("approve: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "new-fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("update runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}

	var out struct {
		Tools []struct {
			Name           string `json:"name"`
			StaleReason    string `json:"stale_reason"`
			StaleReasonKey string `json:"stale_reason_key"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if len(out.Tools) != 1 {
		t.Fatalf("expected 1 tool, got %+v", out.Tools)
	}
	if out.Tools[0].StaleReason == "" {
		t.Fatalf("expected the prose stale_reason to still be present, got %+v", out.Tools[0])
	}
	if out.Tools[0].StaleReasonKey != service.StaleReasonConfigChanged {
		t.Fatalf("expected stale_reason_key %q, got %+v", service.StaleReasonConfigChanged, out.Tools[0])
	}
}

// TestToolsEndpointReportsServerLevelApproved pins that GET .../tools reports
// whether a server-level ('*') grant exists, since listMCPTools's per-tool
// rows alone give the UI no way to know: without this field the settings
// page's server-level toggle always initialized off even when a live grant
// was in force.
func TestToolsEndpointReportsServerLevelApproved(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	decode := func() struct {
		ServerLevelApproved       bool   `json:"server_level_approved"`
		ServerLevelStaleReason    string `json:"server_level_stale_reason"`
		ServerLevelStaleReasonKey string `json:"server_level_stale_reason_key"`
	} {
		t.Helper()
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		req.Header.Set("X-NimoOS-User-ID", "u1")
		rec := httptest.NewRecorder()
		c := e.NewContext(req, rec)
		setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
		if err := h.Tools(c); err != nil {
			t.Fatalf("Tools: %v", err)
		}
		var out struct {
			ServerLevelApproved       bool   `json:"server_level_approved"`
			ServerLevelStaleReason    string `json:"server_level_stale_reason"`
			ServerLevelStaleReasonKey string `json:"server_level_stale_reason_key"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("decode: %v body=%s", err, rec.Body.String())
		}
		return out
	}

	if out := decode(); out.ServerLevelApproved {
		t.Fatalf("expected server_level_approved=false with no wildcard grant, got %+v", out)
	}

	if err := svc.MCPApprovals().PutServerLevel(m.ID, "fp"); err != nil {
		t.Fatalf("grant wildcard: %v", err)
	}
	if out := decode(); !out.ServerLevelApproved || out.ServerLevelStaleReasonKey != "" {
		t.Fatalf("expected server_level_approved=true with no stale reason for a live grant, got %+v", out)
	}

	// Void it: edit the server's identity (config gate), same as the
	// per-tool config-voided test above.
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "new-fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("update runtime: %v", err)
	}
	out := decode()
	if !out.ServerLevelApproved {
		t.Fatalf("expected server_level_approved to stay true even when void (mirrors per-tool rows), got %+v", out)
	}
	if out.ServerLevelStaleReasonKey != service.StaleReasonConfigChanged {
		t.Fatalf("expected server_level_stale_reason_key %q, got %+v", service.StaleReasonConfigChanged, out)
	}
	if out.ServerLevelStaleReason == "" {
		t.Fatalf("expected the prose server_level_stale_reason to also be populated, got %+v", out)
	}
}

// TestApprovalEndpointsRejectMissingUserID pins the auth precondition shared
// by all four public endpoints: without X-NimoOS-User-ID (set by the JWT
// middleware after verifying the caller's bearer token), h.userID fails and
// every one of these handlers must reject with 401 before touching anything
// else.
func TestApprovalEndpointsRejectMissingUserID(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}

	cases := []struct {
		name string
		call func(c echo.Context) error
	}{
		{"Tools", h.Tools},
		{"PutApproval", h.PutApproval},
		{"DeleteApprovals", h.DeleteApprovals},
		{"ListApprovals", h.ListApprovals},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/", strings.NewReader(`{"approved":true}`))
			req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
			// Deliberately no X-NimoOS-User-ID header.
			rec := httptest.NewRecorder()
			c := e.NewContext(req, rec)
			setParams(c, []string{"id", "tool"}, []string{fmt.Sprint(m.ID), "create_issue"})
			err := tc.call(c)
			he, ok := err.(*echo.HTTPError)
			if !ok || he.Code != http.StatusUnauthorized {
				t.Fatalf("%s: expected 401 for missing X-NimoOS-User-ID, got %v", tc.name, err)
			}
		})
	}
}

// TestPutApprovalPercentDecodesWildcard pins that a strictly-encoded "*"
// (sent as "%2A", as a strict URL-encoding client would) still grants the
// server-level approval, exercised through the REAL echo router (not
// SetParamValues) so that Echo's actual param-extraction behavior is in
// play: Echo's c.Param returns the raw percent-encoded segment (see
// TestModelsHandler_Delete_PathParamPreservesEncoding's identical point for
// :name), so without url.PathUnescape this would silently write a junk
// approval literally named "%2A" instead of the wildcard.
func TestPutApprovalPercentDecodesWildcard(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	e.PUT("/mcp/servers/:id/approvals/:tool", h.PutApproval)

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "t", SchemaHash: "sh"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodPut, fmt.Sprintf("/mcp/servers/%d/approvals/%%2A", m.ID), strings.NewReader(`{"approved":true}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	e.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", rec.Code, rec.Body.String())
	}

	rows, err := svc.MCPApprovals().ListForServer(m.ID)
	if err != nil {
		t.Fatalf("ListForServer: %v", err)
	}
	found, junk := false, false
	for _, r := range rows {
		if r.ToolName == "*" {
			found = true
		}
		if r.ToolName == "%2A" {
			junk = true
		}
	}
	if junk {
		t.Fatalf("expected '%%2A' to be decoded to the wildcard, not stored literally: %+v", rows)
	}
	if !found {
		t.Fatalf("expected a server-level '*' approval, got %+v", rows)
	}
}

// TestToolsEndpointReportsListingStaleAfterFailedProbe pins the server-level
// freshness signal the settings UI needs to tell a hot listing from a cold
// one. The per-tool last_seen_at cannot answer this: it lives on the approval
// row, so an unapproved tool has no timestamp at all (pinned by
// TestToolsEndpointReturnsToolsWithApprovalStateAndLastSeenAt above), and the
// UI used to read that zero as "this tool is gone from the server".
func TestToolsEndpointReportsListingStaleAfterFailedProbe(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh1"}},
		"[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	// The listing above is still the newest thing we know, but the most
	// recent probe attempt failed -- what the client is about to render is a
	// leftover, not a live observation.
	if err := svc.MCPRuntime().SaveFailure(m.ID, "connect_failed", "all connection attempts failed"); err != nil {
		t.Fatalf("seed failure: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}

	var out struct {
		ListingStale bool  `json:"listing_stale"`
		LastOkAt     int64 `json:"last_ok_at"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if !out.ListingStale {
		t.Fatalf("expected listing_stale=true after a failed probe, got %s", rec.Body.String())
	}
	if out.LastOkAt == 0 {
		t.Fatalf("expected last_ok_at to carry the earlier success so the UI can say how long it has been cold, got %s", rec.Body.String())
	}
}

// TestToolsEndpointListingNotStaleWhileHealthyServerReprobes guards the
// freshness signal against the transient probe_state='probing' that
// MarkProbing writes at the START of every refresh. Keying listing_stale on
// probe_state alone would flash "this listing may be out of date" at the user
// during each routine re-probe -- constantly for a server whose declared TTL
// sits at the SCHEMA_TTL_MIN floor of 60s. fail_streak is the honest input:
// MarkProbing never touches it, so it still reports the last COMPLETED
// probe's outcome.
func TestToolsEndpointListingNotStaleWhileHealthyServerReprobes(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh1"}},
		"[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	claimed, err := svc.MCPRuntime().MarkProbing(m.ID)
	if err != nil || !claimed {
		t.Fatalf("MarkProbing: claimed=%v err=%v", claimed, err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}

	var out struct {
		ListingStale bool `json:"listing_stale"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if out.ListingStale {
		t.Fatalf("expected listing_stale=false while a healthy server is mid-reprobe, got %s", rec.Body.String())
	}
}

// TestToolsEndpointCarriesToolDescriptions pins the settings UI's ability to
// show WHAT a tool does, and -- next to the existing desc_changed badge --
// what its description changed TO. tools_json holds only name + hashes, so
// the prose has to come from the sibling mcp_server_schemas row. Still zero
// network: both are persisted by the same probe.
func TestToolsEndpointCarriesToolDescriptions(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	schemas := `[{"name":"create_issue","description":"Open a new issue on a repository.","input_schema":{"type":"object"}},
	             {"name":"close_issue","description":"Close an existing issue.","input_schema":{"type":"object"}}]`
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{
			{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh1"},
			{Name: "close_issue", SchemaHash: "sh2", DescHash: "dh2"},
		}, schemas); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(m.ID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}

	var out struct {
		Tools []struct {
			Name        string `json:"name"`
			Description string `json:"description"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	got := map[string]string{}
	for _, tl := range out.Tools {
		got[tl.Name] = tl.Description
	}
	if got["create_issue"] != "Open a new issue on a repository." {
		t.Fatalf("expected create_issue's description, got %+v", got)
	}
	if got["close_issue"] != "Close an existing issue." {
		t.Fatalf("expected close_issue's description, got %+v", got)
	}
}

// seedDescChanged creates one server, approves `tool` against description
// hash `oldDesc`, then re-lists it with `newDesc` — the exact state that
// lights the settings UI's "description changed" badge.
func seedDescChanged(t *testing.T, svc service.Services, tool, oldDesc, newDesc string) *service.McpServer {
	t.Helper()
	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: tool, SchemaHash: "sh1", DescHash: oldDesc}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	if err := svc.MCPApprovals().Put(m.ID, tool, "fp", "sh1", oldDesc); err != nil {
		t.Fatalf("approve: %v", err)
	}
	// Same tool, same schema, new description — desc_hash is the only change.
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: tool, SchemaHash: "sh1", DescHash: newDesc}}, "[]"); err != nil {
		t.Fatalf("re-list: %v", err)
	}
	return m
}

// descChangedOf reads one tool's desc_changed straight off the Tools endpoint,
// so these tests assert on what the settings UI actually receives.
func descChangedOf(t *testing.T, h *MCPHandler, serverID int64, tool string) bool {
	t.Helper()
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id"}, []string{fmt.Sprint(serverID)})
	if err := h.Tools(c); err != nil {
		t.Fatalf("Tools: %v", err)
	}
	var out struct {
		Tools []struct {
			Name        string `json:"name"`
			DescChanged bool   `json:"desc_changed"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	for _, tl := range out.Tools {
		if tl.Name == tool {
			return tl.DescChanged
		}
	}
	t.Fatalf("tool %q not in response %s", tool, rec.Body.String())
	return false
}

// ackDescription drives the endpoint under test.
func ackDescription(t *testing.T, h *MCPHandler, serverID int64, tool string) *httptest.ResponseRecorder {
	t.Helper()
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id", "tool"}, []string{fmt.Sprint(serverID), tool})
	if err := h.AckDescription(c); err != nil {
		if he, ok := err.(*echo.HTTPError); ok {
			rec.Code = he.Code
			return rec
		}
		t.Fatalf("AckDescription: %v", err)
	}
	return rec
}

// TestAckDescriptionClearsTheBadge is the whole point of the endpoint: let the
// user say "I've read it" without re-granting the approval, which is the only
// thing that could clear this badge before (PutApproval's UPSERT re-stamps
// desc_hash as a side effect of re-consenting).
func TestAckDescriptionClearsTheBadge(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	m := seedDescChanged(t, svc, "create_issue", "dh-old", "dh-new")

	if !descChangedOf(t, h, m.ID, "create_issue") {
		t.Fatal("fixture is wrong: desc_changed should be true before the ack")
	}
	if rec := ackDescription(t, h, m.ID, "create_issue"); rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d: %s", rec.Code, rec.Body.String())
	}
	if descChangedOf(t, h, m.ID, "create_issue") {
		t.Fatal("expected desc_changed=false after acknowledging the change")
	}
}

// TestAckDescriptionOnAbsentToolStoresNothing guards the one way this endpoint
// could permanently break the badge. lookupToolMeta returns "" for a tool that
// is not in the CURRENT listing, and desc_changed is reported as
// `stored != "" && stored != current` — so writing that "" would silence the
// badge for this tool forever, even after it comes back with different prose.
// Acking something the server no longer offers must be a no-op.
func TestAckDescriptionOnAbsentToolStoresNothing(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh-old"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	if err := svc.MCPApprovals().Put(m.ID, "create_issue", "fp", "sh1", "dh-old"); err != nil {
		t.Fatalf("approve: %v", err)
	}
	// The server drops create_issue and keeps offering something else (a
	// wholly empty listing would be ignored by SaveSuccess's blip guard).
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "close_issue", SchemaHash: "sh2", DescHash: "dh2"}}, "[]"); err != nil {
		t.Fatalf("re-list: %v", err)
	}

	ackDescription(t, h, m.ID, "create_issue")

	// create_issue returns, with prose that differs from what was approved.
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh-new"}}, "[]"); err != nil {
		t.Fatalf("re-list 2: %v", err)
	}
	if !descChangedOf(t, h, m.ID, "create_issue") {
		t.Fatal("acking an absent tool must not store an empty desc_hash: the badge is now dead for this tool")
	}
}

// TestAckDescriptionRejectsWildcard: '*' is the server-level grant, not a real
// tool, and carries no description of its own (see PutServerLevel). There is
// nothing to acknowledge, and letting it through would write to a row whose
// desc_hash is load-bearing for nothing.
func TestAckDescriptionRejectsWildcard(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	m := seedDescChanged(t, svc, "create_issue", "dh-old", "dh-new")

	if rec := ackDescription(t, h, m.ID, "*"); rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for the wildcard, got %d", rec.Code)
	}
}

// TestAckDescriptionRelightsBadgeOnTheNextChange pins the agreed semantics:
// acknowledging means "I have seen THIS change", not "never mention this tool
// again". Falls out of the desc_hash comparison, which is exactly why it is
// worth pinning -- it is a property of the mechanism, easy to lose in a
// refactor that starts remembering acknowledgements separately.
func TestAckDescriptionRelightsBadgeOnTheNextChange(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	m := seedDescChanged(t, svc, "create_issue", "dh-old", "dh-new")

	ackDescription(t, h, m.ID, "create_issue")
	if descChangedOf(t, h, m.ID, "create_issue") {
		t.Fatal("badge should be clear right after the ack")
	}

	// The server edits the description a second time.
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh1", DescHash: "dh-newer"}}, "[]"); err != nil {
		t.Fatalf("re-list: %v", err)
	}
	if !descChangedOf(t, h, m.ID, "create_issue") {
		t.Fatal("a second, different description must light the badge again")
	}
}

// TestAckDescriptionDoesNotReconsentToAChangedSchema is the security-relevant
// half of "acknowledging is not re-approving". PutApproval's UPSERT re-stamps
// identity_fp AND schema_hash from the current runtime row, so routing an
// acknowledgement through it would silently satisfy the interface gate -- the
// user would have re-consented to a tool whose ARGUMENTS changed by clicking a
// button about its prose. The narrow UPDATE must leave that gate failing.
func TestAckDescriptionDoesNotReconsentToAChangedSchema(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")

	m := &service.McpServer{UserID: "u1", Name: "github", Transport: "http", URL: "https://x", Args: "[]", Env: "{}", Enabled: true}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create server: %v", err)
	}
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh-old", DescHash: "dh-old"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}
	if err := svc.MCPApprovals().Put(m.ID, "create_issue", "fp", "sh-old", "dh-old"); err != nil {
		t.Fatalf("approve: %v", err)
	}
	// Both the arguments and the prose changed.
	if err := svc.MCPRuntime().SaveSuccess(
		&service.McpServerRuntime{ServerID: m.ID, IdentityFP: "fp", TTLSec: 3600},
		[]service.ToolMeta{{Name: "create_issue", SchemaHash: "sh-new", DescHash: "dh-new"}}, "[]"); err != nil {
		t.Fatalf("re-list: %v", err)
	}

	ackDescription(t, h, m.ID, "create_issue")

	rows, err := svc.MCPApprovals().ListForServer(m.ID)
	if err != nil {
		t.Fatalf("ListForServer: %v", err)
	}
	var found bool
	for _, r := range rows {
		if r.ToolName != "create_issue" {
			continue
		}
		found = true
		if r.StaleReasonKey != service.StaleReasonSchemaChanged {
			t.Fatalf("acking the description must leave the interface gate failing, got stale_reason_key=%q", r.StaleReasonKey)
		}
	}
	if !found {
		t.Fatalf("approval row vanished: %+v", rows)
	}
}

// TestAckDescriptionRejectsForeignServer: same indistinguishable-403 rule as
// every other handler here -- a server that belongs to someone else and one
// that does not exist must not be tellable apart.
func TestAckDescriptionRejectsForeignServer(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	m := seedDescChanged(t, svc, "create_issue", "dh-old", "dh-new")

	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "someone-else")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	setParams(c, []string{"id", "tool"}, []string{fmt.Sprint(m.ID), "create_issue"})
	err := h.AckDescription(c)
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %v", err)
	}
	if descChangedOf(t, h, m.ID, "create_issue") != true {
		t.Fatal("a rejected ack must not have cleared the owner's badge")
	}
}
