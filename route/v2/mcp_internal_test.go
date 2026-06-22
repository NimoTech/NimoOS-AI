package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/labstack/echo/v4"
)

func TestRegisterInternal_CreatesForUser(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/register",
		strings.NewReader(`{"user_id":"7","command_line":"npx -y @upstash/context7-mcp"}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	if err := h.RegisterInternal(e.NewContext(req, rec)); err != nil {
		t.Fatalf("err: %v", err)
	}
	if rec.Code != http.StatusCreated {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	var out struct {
		ID        int64  `json:"id"`
		Name      string `json:"name"`
		Transport string `json:"transport"`
		Command   string `json:"command"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	if out.Command != "npx" || out.Name != "context7" || out.Transport != "stdio" {
		t.Fatalf("got %+v", out)
	}
	rows, err := svc.MCP().ListMcpServers("7")
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("expected 1 server for user 7, got %d", len(rows))
	}
}

func TestRegisterInternal_RequiresUserID(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/register",
		strings.NewReader(`{"command_line":"npx x"}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	err := h.RegisterInternal(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestParseInternal_OK(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/parse",
		strings.NewReader(`{"command_line":"uvx mcp-server-time"}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	if err := h.ParseInternal(e.NewContext(req, rec)); err != nil {
		t.Fatalf("err: %v", err)
	}
	var out struct {
		Command string `json:"command"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	if out.Command != "uvx" {
		t.Fatalf("got %+v", out)
	}
}

func TestListInternal_RequiresUserID(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/v1/ai/_internal/mcp/list", nil)
	rec := httptest.NewRecorder()
	err := h.ListInternal(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestListInternal_ReturnsDTOs(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	// seed one server for user 7 via the register endpoint
	regReq := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/register",
		strings.NewReader(`{"user_id":"7","command_line":"npx -y @pkg"}`))
	regReq.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	regRec := httptest.NewRecorder()
	if err := h.RegisterInternal(e.NewContext(regReq, regRec)); err != nil {
		t.Fatalf("seed: %v", err)
	}
	// list
	req := httptest.NewRequest(http.MethodGet, "/v1/ai/_internal/mcp/list?user_id=7", nil)
	rec := httptest.NewRecorder()
	if err := h.ListInternal(e.NewContext(req, rec)); err != nil {
		t.Fatalf("err: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d", rec.Code)
	}
	var arr []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &arr); err != nil {
		t.Fatalf("not an array: %s", rec.Body.String())
	}
	if len(arr) != 1 {
		t.Fatalf("expected 1 dto, got %d: %s", len(arr), rec.Body.String())
	}
}

func TestRemoveInternal_RequiresUserID(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/remove",
		strings.NewReader(`{"id":3}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	err := h.RemoveInternal(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestRemoveInternal_Deletes(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	// seed
	regReq := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/register",
		strings.NewReader(`{"user_id":"7","command_line":"npx -y @pkg"}`))
	regReq.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	regRec := httptest.NewRecorder()
	if err := h.RegisterInternal(e.NewContext(regReq, regRec)); err != nil {
		t.Fatalf("seed: %v", err)
	}
	var created struct {
		ID int64 `json:"id"`
	}
	_ = json.Unmarshal(regRec.Body.Bytes(), &created)
	// remove
	body := `{"user_id":"7","id":` + strconv.FormatInt(created.ID, 10) + `}`
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/remove",
		strings.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	if err := h.RemoveInternal(e.NewContext(req, rec)); err != nil {
		t.Fatalf("remove err: %v", err)
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("code=%d", rec.Code)
	}
	rows, _ := svc.MCP().ListMcpServers("7")
	if len(rows) != 0 {
		t.Fatalf("expected 0 after remove, got %d", len(rows))
	}
}

func TestRegisterInternal_RequiresCommandLine(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/register",
		strings.NewReader(`{"user_id":"7"}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	err := h.RegisterInternal(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestRemoveInternal_RequiresID(t *testing.T) {
	svc := mcpTestSvc(t)
	h := NewMCPHandler(svc, NewTicketStore(time.Minute), "http://127.0.0.1:1")
	e := echo.New()
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/mcp/remove",
		strings.NewReader(`{"user_id":"7"}`))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	err := h.RemoveInternal(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}
