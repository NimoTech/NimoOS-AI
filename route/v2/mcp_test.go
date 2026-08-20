package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/NimoTech/NimoOS-AI/pkg/crypto"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

func mcpTestSvc(t *testing.T) service.Services {
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
	return service.NewServicesForTest(db, mk)
}

func TestMcpHandler_CreateListDTOHidesSecrets(t *testing.T) {
	svc := mcpTestSvc(t)
	ts := NewTicketStore(time.Minute)
	h := NewMCPHandler(svc, ts, NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	body := `{"name":"github","transport":"http","url":"https://x","headers":{"Authorization":"Bearer SECRET"}}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	if err := h.Create(e.NewContext(req, rec)); err != nil {
		t.Fatalf("create: %v", err)
	}
	if rec.Code != http.StatusCreated {
		t.Fatalf("create code %d", rec.Code)
	}

	req2 := httptest.NewRequest(http.MethodGet, "/", nil)
	req2.Header.Set("X-NimoOS-User-ID", "u1")
	rec2 := httptest.NewRecorder()
	if err := h.List(e.NewContext(req2, rec2)); err != nil {
		t.Fatalf("list: %v", err)
	}
	if strings.Contains(rec2.Body.String(), "SECRET") || strings.Contains(rec2.Body.String(), "Bearer") {
		t.Fatalf("DTO leaked secret: %s", rec2.Body.String())
	}
	if !strings.Contains(rec2.Body.String(), `"has_headers":true`) {
		t.Fatalf("expected has_headers flag: %s", rec2.Body.String())
	}
}

func TestMcpHandler_RuntimeReturnsDecryptedForTicket(t *testing.T) {
	svc := mcpTestSvc(t)
	ts := NewTicketStore(time.Minute)
	h := NewMCPHandler(svc, ts, NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	enc, _ := svc.MasterKey().Encrypt(`{"Authorization":"Bearer SECRET"}`)
	m := &service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Headers: enc, Enabled: true,
	}
	_ = svc.MCP().CreateMcpServer(m)
	// Task 8's TTL self-check treats a server with no runtime row as
	// trivially expired and fires a background probe against agentURL
	// ("http://127.0.0.1:1" above, deliberately unreachable). Seed a fresh,
	// long-TTL runtime row so that pre-filter skips this server: this test
	// is about decryption, not the self-check, and an unwanted goroutine
	// would otherwise call SaveFailure after t.Cleanup has closed the DB —
	// harmless in production but noisy/racy here.
	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{ServerID: m.ID, TTLSec: 3600},
		[]service.ToolMeta{{Name: "noop", SchemaHash: "h", DescHash: "d"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	tok := ts.Mint("u1")
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Agent-MCP-Ticket", tok)
	rec := httptest.NewRecorder()
	if err := h.Runtime(e.NewContext(req, rec)); err != nil {
		t.Fatalf("runtime: %v", err)
	}
	var out struct {
		Servers []struct {
			Name    string            `json:"name"`
			Headers map[string]string `json:"headers"`
		} `json:"servers"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if len(out.Servers) != 1 || out.Servers[0].Headers["Authorization"] != "Bearer SECRET" {
		t.Fatalf("runtime did not decrypt: %+v", out)
	}
	if strings.Contains(rec.Body.String(), "config_error") {
		t.Fatalf("healthy server must not carry config_error: %s", rec.Body.String())
	}

	rec2 := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodGet, "/", nil)
	req2.Header.Set("X-Agent-MCP-Ticket", tok)
	_ = h.Runtime(e.NewContext(req2, rec2))
	if rec2.Code != http.StatusUnauthorized {
		t.Fatalf("reused ticket should 401, got %d", rec2.Code)
	}
}

func TestMcpHandler_RuntimeMarksUndecryptableConfig(t *testing.T) {
	svc := mcpTestSvc(t)
	ts := NewTicketStore(time.Minute)
	h := NewMCPHandler(svc, ts, NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()

	// Headers is non-empty but NOT ciphertext produced by this master key, so
	// decryption fails — the defect-1 silent point 3 scenario.
	m := &service.McpServer{
		UserID: "u1", Name: "broken", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Headers: "garbage-not-ciphertext", Enabled: true,
	}
	_ = svc.MCP().CreateMcpServer(m)
	// A decrypt failure already makes Task 8's TTL self-check skip this
	// server (probing with known-broken credentials would just fail
	// predictably), but seed a fresh, long-TTL runtime row anyway so this
	// test's cleanliness does not depend on that guard remaining in place —
	// see TestMcpHandler_RuntimeReturnsDecryptedForTicket's identical comment
	// for what goes wrong otherwise.
	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{ServerID: m.ID, TTLSec: 3600},
		[]service.ToolMeta{{Name: "noop", SchemaHash: "h", DescHash: "d"}}, "[]"); err != nil {
		t.Fatalf("seed runtime: %v", err)
	}

	tok := ts.Mint("u1")
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Agent-MCP-Ticket", tok)
	rec := httptest.NewRecorder()
	if err := h.Runtime(e.NewContext(req, rec)); err != nil {
		t.Fatalf("runtime: %v", err)
	}
	var out struct {
		Servers []struct {
			Name        string            `json:"name"`
			Headers     map[string]string `json:"headers"`
			ConfigError string            `json:"config_error"`
		} `json:"servers"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	if len(out.Servers) != 1 || out.Servers[0].ConfigError == "" {
		t.Fatalf("expected config_error to be set: %s", rec.Body.String())
	}
	if len(out.Servers[0].Headers) != 0 {
		t.Fatalf("an undecryptable config must not ship (unauthenticated) headers: %+v",
			out.Servers[0].Headers)
	}
}

func TestMcpHandler_CreateStdioAcceptedAndCleansURL(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	body := `{"name":"fs","transport":"stdio","command":"npx","args":["-y","x"],"url":"https://stray"}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	if err := h.Create(e.NewContext(req, rec)); err != nil {
		t.Fatalf("create: %v", err)
	}
	if rec.Code != http.StatusCreated {
		t.Fatalf("code %d body %s", rec.Code, rec.Body.String())
	}
	rows, _ := svc.MCP().ListMcpServers("u1")
	if len(rows) != 1 || rows[0].Transport != "stdio" || rows[0].Command != "npx" {
		t.Fatalf("stored wrong: %+v", rows[0])
	}
	if rows[0].URL != "" {
		t.Fatalf("stray url not cleared: %q", rows[0].URL)
	}
}

func TestMcpHandler_CreateStdioRequiresCommand(t *testing.T) {
	h := NewMCPHandler(mcpTestSvc(t), NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	body := `{"name":"x","transport":"stdio"}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	err := h.Create(e.NewContext(req, rec))
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestMcpHandler_CreateUnknownTransport(t *testing.T) {
	h := NewMCPHandler(mcpTestSvc(t), NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	body := `{"name":"x","transport":"ws","url":"https://x"}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	err := h.Create(e.NewContext(req, rec))
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestMcpHandler_CreateHTTPRequiresURL(t *testing.T) {
	h := NewMCPHandler(mcpTestSvc(t), NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	body := `{"name":"x","transport":"http"}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	err := h.Create(e.NewContext(req, rec))
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestMcpHandler_TestProxiesToAgent(t *testing.T) {
	svc := mcpTestSvc(t)
	enc, _ := svc.MasterKey().Encrypt(`{"Authorization":"Bearer S"}`)
	_ = svc.MCP().CreateMcpServer(&service.McpServer{
		UserID: "u1", Name: "github", Transport: "http", URL: "https://x",
		Args: "[]", Env: "{}", Headers: enc, Enabled: true,
	})
	var gotAuth string
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if h, ok := body["headers"].(map[string]any); ok {
			gotAuth, _ = h["Authorization"].(string)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"tool_count":2,"tools":["a","b"],"protocol_era":"modern","protocol_version":"2026-07-28","supported_versions":["2026-07-28","2025-11-25"]}`))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues("1")
	if err := h.Test(c); err != nil {
		t.Fatalf("Test: %v", err)
	}
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"tool_count":2`) {
		t.Fatalf("unexpected resp %d %s", rec.Code, rec.Body.String())
	}
	if gotAuth != "Bearer S" {
		t.Fatalf("agent did not receive decrypted header, got %q", gotAuth)
	}

	// Task 3: the handler proxies the agent's JSON verbatim (c.JSONBlob), so
	// new protocol-version fields must pass through untouched rather than
	// being silently dropped by some future typed deserialization.
	var respBody map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &respBody); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	if respBody["protocol_era"] != "modern" {
		t.Fatalf("expected protocol_era to pass through, got %v", respBody["protocol_era"])
	}
	if respBody["protocol_version"] != "2026-07-28" {
		t.Fatalf("expected protocol_version to pass through, got %v", respBody["protocol_version"])
	}
	supported, ok := respBody["supported_versions"].([]any)
	if !ok || len(supported) != 2 || supported[0] != "2026-07-28" || supported[1] != "2025-11-25" {
		t.Fatalf("expected supported_versions to pass through, got %v", respBody["supported_versions"])
	}
}

func TestMcpHandler_TestNotFound(t *testing.T) {
	h := NewMCPHandler(mcpTestSvc(t), NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues("999")
	err := h.Test(c)
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %v", err)
	}
}
