package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/labstack/echo/v4"
)

func TestParseHandler_Stdio(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/mcp/servers/parse",
		strings.NewReader(`{"command_line":"npx -y @upstash/context7-mcp"}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	if err := h.Parse(e.NewContext(req, rec)); err != nil {
		t.Fatalf("handler err: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	var out struct {
		Transport     string   `json:"transport"`
		Command       string   `json:"command"`
		SuggestedName string   `json:"suggested_name"`
		Args          []string `json:"args"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	if out.Transport != "stdio" || out.Command != "npx" || out.SuggestedName != "context7" {
		t.Fatalf("got %+v", out)
	}
}

func TestParseHandler_BadInput(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/mcp/servers/parse",
		strings.NewReader(`{"command_line":"  "}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	err := h.Parse(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 HTTPError, got %v", err)
	}
}

func TestCreate_FromCommandLine(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/mcp/servers",
		strings.NewReader(`{"command_line":"npx -y @upstash/context7-mcp","name":"ctx"}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	req.Header.Set("X-NimoOS-User-ID", "1")
	rec := httptest.NewRecorder()
	if err := h.Create(e.NewContext(req, rec)); err != nil {
		t.Fatalf("create err: %v", err)
	}
	if rec.Code != http.StatusCreated {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	// Assert the stored server via the real DB: must be stdio/npx with explicit name "ctx".
	rows, err := svc.MCP().ListMcpServers("1")
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("expected 1 server, got %d", len(rows))
	}
	if rows[0].Transport != "stdio" || rows[0].Command != "npx" || rows[0].Name != "ctx" {
		t.Fatalf("stored: %+v", rows[0])
	}
}
