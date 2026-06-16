package v2

import (
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type MCPHandler struct {
	svc     service.Services
	tickets *TicketStore
}

func NewMCPHandler(svc service.Services, tickets *TicketStore) *MCPHandler {
	return &MCPHandler{svc: svc, tickets: tickets}
}

type mcpRequest struct {
	Name      *string            `json:"name"`
	Transport *string            `json:"transport"`
	URL       *string            `json:"url"`
	Command   *string            `json:"command"`
	Args      *[]string          `json:"args"`
	Env       *map[string]string `json:"env"`     // plaintext in; handler encrypts
	Headers   *map[string]string `json:"headers"` // plaintext in; handler encrypts
	Enabled   *bool              `json:"enabled"`
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
	m := &service.McpServer{UserID: uid, Transport: "http", Args: "[]", Env: "{}", Enabled: true}
	if err := h.applyReq(m, &req); err != nil {
		return err
	}
	if m.Transport != "http" && m.Transport != "sse" {
		return echo.NewHTTPError(http.StatusBadRequest, "transport must be 'http' or 'sse' in phase 1")
	}
	if m.URL == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "url required")
	}
	if err := h.svc.MCP().CreateMcpServer(m); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusCreated, map[string]int64{"id": m.ID})
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
