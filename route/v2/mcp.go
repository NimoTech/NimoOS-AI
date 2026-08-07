package v2

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/NimoTech/NimoOS-AI/pkg/mcpparse"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type MCPHandler struct {
	svc      service.Services
	tickets  *TicketStore
	agentURL string
}

func NewMCPHandler(svc service.Services, tickets *TicketStore, agentURL string) *MCPHandler {
	return &MCPHandler{svc: svc, tickets: tickets, agentURL: agentURL}
}

type mcpRequest struct {
	Name        *string            `json:"name"`
	Transport   *string            `json:"transport"`
	URL         *string            `json:"url"`
	Command     *string            `json:"command"`
	Args        *[]string          `json:"args"`
	Env         *map[string]string `json:"env"`     // plaintext in; handler encrypts
	Headers     *map[string]string `json:"headers"` // plaintext in; handler encrypts
	Enabled     *bool              `json:"enabled"`
	CommandLine *string            `json:"command_line"` // optional: parsed to fill transport/command/args/url/env
}

type mcpDTO struct {
	ID         int64    `json:"id"`
	Name       string   `json:"name"`
	Transport  string   `json:"transport"`
	URL        string   `json:"url"`
	Command    string   `json:"command"`
	Args       []string `json:"args"`
	Enabled    bool     `json:"enabled"`
	HasHeaders bool     `json:"has_headers"`
	HasEnv     bool     `json:"has_env"`
}

func toMcpDTO(m *service.McpServer) mcpDTO {
	var args []string
	_ = json.Unmarshal([]byte(m.Args), &args)
	if args == nil {
		args = []string{}
	}
	return mcpDTO{
		ID: m.ID, Name: m.Name, Transport: m.Transport, URL: m.URL,
		Command: m.Command, Args: args, Enabled: m.Enabled,
		HasHeaders: m.Headers != "", HasEnv: m.Env != "" && m.Env != "{}",
	}
}

func (h *MCPHandler) userID(c echo.Context) (string, error) {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return "", echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	return uid, nil
}

// encryptMap returns AES-GCM ciphertext of the JSON-encoded map, or "" if empty.
func (h *MCPHandler) encryptMap(m *map[string]string) (string, error) {
	if m == nil || len(*m) == 0 {
		return "", nil
	}
	buf, _ := json.Marshal(*m)
	return h.svc.MasterKey().Encrypt(string(buf))
}

func (h *MCPHandler) List(c echo.Context) error {
	uid, err := h.userID(c)
	if err != nil {
		return err
	}
	rows, err := h.svc.MCP().ListMcpServers(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	out := make([]mcpDTO, len(rows))
	for i, m := range rows {
		out[i] = toMcpDTO(m)
	}
	return c.JSON(http.StatusOK, out)
}

func (h *MCPHandler) Create(c echo.Context) error {
	uid, err := h.userID(c)
	if err != nil {
		return err
	}
	var req mcpRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if err := applyCommandLine(&req); err != nil {
		return err
	}
	m := &service.McpServer{UserID: uid, Transport: "http", Args: "[]", Env: "{}", Enabled: true}
	if err := h.applyReq(m, &req); err != nil {
		return err
	}
	if err := validateAndClean(m); err != nil {
		return err
	}
	if err := h.svc.MCP().CreateMcpServer(m); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusCreated, map[string]int64{"id": m.ID})
}

// Parse handles POST /v1/ai/mcp/servers/parse — parse a command line into a
// server config WITHOUT persisting. Used by the UI to prefill the add form.
func (h *MCPHandler) Parse(c echo.Context) error {
	var body struct {
		CommandLine string `json:"command_line"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	p, err := mcpparse.Parse(body.CommandLine)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	return c.JSON(http.StatusOK, p)
}

func (h *MCPHandler) Update(c echo.Context) error {
	uid, err := h.userID(c)
	if err != nil {
		return err
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	existing, err := h.svc.MCP().GetMcpServer(id, uid)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "mcp server not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	var req mcpRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if err := h.applyReq(existing, &req); err != nil {
		return err
	}
	if err := validateAndClean(existing); err != nil {
		return err
	}
	if err := h.svc.MCP().UpdateMcpServer(existing); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "mcp server not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

func (h *MCPHandler) Delete(c echo.Context) error {
	uid, err := h.userID(c)
	if err != nil {
		return err
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	if err := h.svc.MCP().DeleteMcpServer(id, uid); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "mcp server not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

// applyCommandLine parses req.CommandLine (if present) into req's transport/
// command/args/url/env BEFORE applyReq runs, so explicit fields still override.
// SuggestedName only fills req.Name when the caller gave no name.
func applyCommandLine(req *mcpRequest) error {
	if req.CommandLine == nil || strings.TrimSpace(*req.CommandLine) == "" {
		return nil
	}
	p, err := mcpparse.Parse(*req.CommandLine)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if req.Transport == nil {
		req.Transport = &p.Transport
	}
	if req.URL == nil && p.URL != "" {
		req.URL = &p.URL
	}
	if req.Command == nil && p.Command != "" {
		req.Command = &p.Command
	}
	if req.Args == nil && p.Transport == "stdio" {
		args := p.Args
		req.Args = &args
	}
	if req.Env == nil && len(p.Env) > 0 {
		env := p.Env
		req.Env = &env
	}
	if req.Name == nil && p.SuggestedName != "" {
		name := p.SuggestedName
		req.Name = &name
	}
	return nil
}

// applyReq merges request fields into m, encrypting headers/env. Only fields
// present in the request are overwritten.
func (h *MCPHandler) applyReq(m *service.McpServer, req *mcpRequest) error {
	if req.Name != nil {
		m.Name = *req.Name
	}
	if req.Transport != nil {
		m.Transport = *req.Transport
	}
	if req.URL != nil {
		m.URL = *req.URL
	}
	if req.Command != nil {
		m.Command = *req.Command
	}
	if req.Args != nil {
		buf, _ := json.Marshal(*req.Args)
		m.Args = string(buf)
	}
	if req.Headers != nil {
		enc, err := h.encryptMap(req.Headers)
		if err != nil {
			return echo.NewHTTPError(http.StatusInternalServerError, "failed to encrypt headers")
		}
		m.Headers = enc
	}
	if req.Env != nil {
		enc, err := h.encryptMap(req.Env)
		if err != nil {
			return echo.NewHTTPError(http.StatusInternalServerError, "failed to encrypt env")
		}
		if enc == "" {
			m.Env = "{}"
		} else {
			m.Env = enc
		}
	}
	if req.Enabled != nil {
		m.Enabled = *req.Enabled
	}
	return nil
}

// validateAndClean enforces per-transport required fields and clears the fields
// that don't belong to the chosen transport (no dirty url+command rows).
func validateAndClean(m *service.McpServer) error {
	switch m.Transport {
	case "http", "sse":
		if m.URL == "" {
			return echo.NewHTTPError(http.StatusBadRequest, "url required for http/sse")
		}
		m.Command, m.Args, m.Env = "", "[]", "{}"
	case "stdio":
		if m.Command == "" {
			return echo.NewHTTPError(http.StatusBadRequest, "command required for stdio")
		}
		m.URL, m.Headers = "", ""
	default:
		return echo.NewHTTPError(http.StatusBadRequest, "transport must be 'http', 'sse' or 'stdio'")
	}
	return nil
}

// --- internal loopback runtime endpoint ---

type runtimeServer struct {
	ID        int64             `json:"id"`
	Name      string            `json:"name"`
	Transport string            `json:"transport"`
	URL       string            `json:"url"`
	Command   string            `json:"command"`
	Args      []string          `json:"args"`
	Env       map[string]string `json:"env"`
	Headers   map[string]string `json:"headers"`
}

func (h *MCPHandler) decryptMap(enc string) map[string]string {
	out := map[string]string{}
	if enc == "" || enc == "{}" {
		return out
	}
	plain, err := h.svc.MasterKey().Decrypt(enc)
	if err != nil {
		return out
	}
	_ = json.Unmarshal([]byte(plain), &out)
	return out
}

// Test handles POST /v1/ai/mcp/servers/:id/test — connectivity probe. Go can't
// speak MCP, so it decrypts the saved config and forwards to the Python agent's
// /agent/mcp/test, returning the agent's {ok,tool_count,tools,protocol_era,protocol_version,supported_versions,error} verbatim.
func (h *MCPHandler) Test(c echo.Context) error {
	uid, err := h.userID(c)
	if err != nil {
		return err
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	m, err := h.svc.MCP().GetMcpServer(id, uid)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "mcp server not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	var args []string
	_ = json.Unmarshal([]byte(m.Args), &args)
	payload, _ := json.Marshal(map[string]any{
		"id": m.ID, "name": m.Name, "transport": m.Transport, "url": m.URL,
		"command": m.Command, "args": args,
		"env":     h.decryptMap(m.Env),
		"headers": h.decryptMap(m.Headers),
	})
	// Must exceed Python's outer backstop (client.py: TEST_TIMEOUT=41 /
	// STDIO_TEST_TIMEOUT=120) so Python cancels first and releases the subprocess and
	// socket, rather than Go abandoning a request that keeps running. Those Python
	// values are themselves connect phase + list phase + close phase + slack -- see
	// the constant block in client.py.
	timeout := 43 * time.Second
	if m.Transport == "stdio" {
		timeout = 125 * time.Second
	}
	client := &http.Client{Timeout: timeout}
	resp, err := client.Post(h.agentURL+"/agent/mcp/test", "application/json", bytes.NewReader(payload))
	if err != nil {
		return c.JSON(http.StatusBadGateway, map[string]any{"ok": false, "error": "agent unreachable"})
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return c.JSONBlob(http.StatusOK, body)
}

// --- internal loopback CRUD-lite (no JWT; localhost-only via _internal group) ---
// user_id comes from the request body/query (caller is a local, trusted process:
// the CLI or the agent), NOT from JWT claims.

// ParseInternal handles POST /v1/ai/_internal/mcp/parse — same as public Parse,
// for non-JWT local callers (the agent, building its confirm card).
func (h *MCPHandler) ParseInternal(c echo.Context) error {
	return h.Parse(c)
}

// RegisterInternal handles POST /v1/ai/_internal/mcp/register — parse a command
// line and create a server for the given user_id. Used by the CLI and the agent.
func (h *MCPHandler) RegisterInternal(c echo.Context) error {
	var body struct {
		UserID      string `json:"user_id"`
		CommandLine string `json:"command_line"`
		Name        string `json:"name"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if strings.TrimSpace(body.UserID) == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "user_id required")
	}
	if strings.TrimSpace(body.CommandLine) == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "command_line required")
	}
	req := mcpRequest{CommandLine: &body.CommandLine}
	if strings.TrimSpace(body.Name) != "" {
		req.Name = &body.Name
	}
	if err := applyCommandLine(&req); err != nil {
		return err
	}
	m := &service.McpServer{UserID: body.UserID, Transport: "http", Args: "[]", Env: "{}", Enabled: true}
	if err := h.applyReq(m, &req); err != nil {
		return err
	}
	if err := validateAndClean(m); err != nil {
		return err
	}
	if err := h.svc.MCP().CreateMcpServer(m); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusCreated, toMcpDTO(m))
}

// ListInternal handles GET /v1/ai/_internal/mcp/list?user_id=<id> — DTOs (no
// secrets) for the given user. Used by the CLI `list`.
func (h *MCPHandler) ListInternal(c echo.Context) error {
	uid := c.QueryParam("user_id")
	if strings.TrimSpace(uid) == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "user_id required")
	}
	rows, err := h.svc.MCP().ListMcpServers(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	out := make([]mcpDTO, len(rows))
	for i, m := range rows {
		out[i] = toMcpDTO(m)
	}
	return c.JSON(http.StatusOK, out)
}

// RemoveInternal handles POST /v1/ai/_internal/mcp/remove — delete (id,user_id).
// Used by the CLI `remove`. Reuses DeleteMcpServer.
func (h *MCPHandler) RemoveInternal(c echo.Context) error {
	var body struct {
		UserID string `json:"user_id"`
		ID     int64  `json:"id"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if strings.TrimSpace(body.UserID) == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "user_id required")
	}
	if body.ID == 0 {
		return echo.NewHTTPError(http.StatusBadRequest, "id required")
	}
	if err := h.svc.MCP().DeleteMcpServer(body.ID, body.UserID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "mcp server not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

// Runtime serves GET /v1/ai/_internal/mcp/runtime. Auth is the one-time ticket
// (minted by the agent Proxy), NOT X-NimoOS-User-ID. Localhost-only is enforced
// by the _internal group's LocalhostOnly middleware.
func (h *MCPHandler) Runtime(c echo.Context) error {
	tok := c.Request().Header.Get("X-Agent-MCP-Ticket")
	uid, ok := h.tickets.Resolve(tok)
	if !ok {
		// Write the 401 directly so that callers checking rec.Code without running
		// Echo's error handler see the correct status (e.g. in direct handler tests).
		return c.JSON(http.StatusUnauthorized, map[string]string{"message": "invalid mcp ticket"})
	}
	rows, err := h.svc.MCP().ListEnabledMcpServers(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	servers := make([]runtimeServer, 0, len(rows))
	for _, m := range rows {
		var args []string
		_ = json.Unmarshal([]byte(m.Args), &args)
		servers = append(servers, runtimeServer{
			ID: m.ID, Name: m.Name, Transport: m.Transport, URL: m.URL,
			Command: m.Command, Args: args,
			Env:     h.decryptMap(m.Env),
			Headers: h.decryptMap(m.Headers),
		})
	}
	return c.JSON(http.StatusOK, map[string]any{"servers": servers})
}
