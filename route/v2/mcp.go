package v2

import (
	"database/sql"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/NimoTech/NimoOS-AI/pkg/mcpparse"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type MCPHandler struct {
	svc       service.Services
	tickets   *TicketStore
	runTokens *RunTokenStore
	agentURL  string
}

func NewMCPHandler(svc service.Services, tickets *TicketStore, runTokens *RunTokenStore, agentURL string) *MCPHandler {
	return &MCPHandler{svc: svc, tickets: tickets, runTokens: runTokens, agentURL: agentURL}
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
	// Set when the stored env/headers ciphertext failed to decrypt. The agent
	// must not connect with an unauthenticated config — a 401 at call time
	// would mask the real cause (the stored credentials are broken).
	ConfigError string `json:"config_error,omitempty"`
	// UpdatedAt is mcp_servers.updated_at (the config row itself, not the
	// probe observation below) — when the user last edited this server's
	// settings. Nothing consumes it yet; it ships because the brief's
	// Interfaces block lists it alongside the identity card fields.
	UpdatedAt int64 `json:"updated_at"`

	// Everything below is the identity card + health observation persisted by
	// probeAndPersist (Task 7) into mcp_server_runtime. This is the whole
	// point of this endpoint: the agent fetches it once at run start and
	// never has to ask again for the rest of the run (progressive disclosure
	// design doc §2). Fields are zero-valued (never omitted) when the server
	// has no runtime row yet — "never probed" is a normal state, not an
	// error, and the agent must be able to tell "no listing yet" (ttl_sec==0,
	// listed_at==0) apart from "listing is empty".
	Handle        string             `json:"handle"`
	Summary       string             `json:"summary"`
	Instructions  string             `json:"instructions"`
	Tools         []service.ToolMeta `json:"tools"`
	ListedAt      int64              `json:"listed_at"`
	TTLSec        int64              `json:"ttl_sec"`
	ProtocolMode  string             `json:"protocol_mode"`
	ProbeState    string             `json:"probe_state"`
	LastError     string             `json:"last_error"`
	LastErrorKey  string             `json:"last_error_key"`
	CooldownUntil int64              `json:"cooldown_until"`
}

// approvalDTO is one element of the Runtime response's top-level "approvals"
// array: the already-gated (EffectiveApprovals) set the agent may act on
// without asking again this run. StaleReason is display-only (settings page)
// and must never reach the agent — omitted from this DTO on purpose.
type approvalDTO struct {
	ServerID int64  `json:"server_id"`
	ToolName string `json:"tool_name"`
}

func (h *MCPHandler) decryptMapErr(enc string) (map[string]string, error) {
	out := map[string]string{}
	if enc == "" || enc == "{}" {
		return out, nil
	}
	plain, err := h.svc.MasterKey().Decrypt(enc)
	if err != nil {
		return out, err
	}
	_ = json.Unmarshal([]byte(plain), &out)
	return out, nil
}

// decryptMap keeps the old silent behaviour for callers where a partial
// result is acceptable (the /test probe surfaces its own failure).
func (h *MCPHandler) decryptMap(enc string) map[string]string {
	out, _ := h.decryptMapErr(enc)
	return out
}

// Test handles POST /v1/ai/mcp/servers/:id/test — connectivity probe. Go can't
// speak MCP, so it decrypts the saved config and forwards to the Python agent's
// /agent/mcp/test via probeAndPersist, which persists the resulting identity
// card (handle, summary, tool list, protocol era/mode, fingerprints) BEFORE
// this returns the agent's
// {ok,tool_count,tools,protocol_era,protocol_version,supported_versions,error}
// verbatim to the browser — the settings page still shows the outcome
// synchronously, it just now also lands in mcp_server_runtime.
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
	pr, err := h.probeAndPersist(m, h.decryptMap(m.Env), h.decryptMap(m.Headers))
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSONBlob(pr.StatusCode, pr.Body)
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
//
// This is the one request the agent makes at run start; everything the model
// will know about MCP for the rest of the run comes from this single
// response — identity cards (handle/summary/instructions/tools), the
// pre-filtered approval set, and a run-scoped write token all ship together
// so the agent never needs a second round-trip mid-run (progressive
// disclosure design doc §2, §5.2, §5.4).
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
	// One query for every server's runtime row, keyed by server_id, instead
	// of a Get() per server in the loop below.
	runtimeRows, err := h.svc.MCPRuntime().List(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}

	now := time.Now().Unix()
	servers := make([]runtimeServer, 0, len(rows))
	for _, m := range rows {
		var args []string
		_ = json.Unmarshal([]byte(m.Args), &args)
		env, envErr := h.decryptMapErr(m.Env)
		headers, hdrErr := h.decryptMapErr(m.Headers)
		rs := runtimeServer{
			ID: m.ID, Name: m.Name, Transport: m.Transport, URL: m.URL,
			Command: m.Command, Args: args,
			Env: env, Headers: headers,
			UpdatedAt: m.UpdatedAt,
			Tools:     []service.ToolMeta{},
		}
		if envErr != nil || hdrErr != nil {
			rs.Env, rs.Headers = map[string]string{}, map[string]string{}
			rs.ConfigError = "stored credentials could not be decrypted; ask the user to re-save this server's headers/env"
		}

		// rt is nil when this server has never had a successful probe — a
		// normal "no observation yet" state (Task 4), not an error. Treat it
		// like an all-zero row: listed_at==0/ttl_sec==0 makes the TTL check
		// below trivially true, so a brand-new server also gets its first
		// background probe kicked off here rather than staying dark forever.
		rt := runtimeRows[m.ID]
		if rt != nil {
			var tools []service.ToolMeta
			_ = json.Unmarshal([]byte(rt.ToolsJSON), &tools)
			if tools == nil {
				tools = []service.ToolMeta{}
			}
			rs.Handle = rt.Handle
			rs.Summary = rt.Summary
			rs.Instructions = rt.Instructions
			rs.Tools = tools
			rs.ListedAt = rt.ListedAt
			rs.TTLSec = rt.TTLSec
			rs.ProtocolMode = rt.ProtocolMode
			rs.ProbeState = rt.ProbeState
			rs.LastError = rt.LastError
			rs.LastErrorKey = rt.LastErrorKey
			rs.CooldownUntil = rt.CooldownUntil
		}

		// TTL self-check: Go notices an expired listing right here, while it
		// already has the runtime row in hand, and kicks off a background
		// refresh WITHOUT waiting for it. This request ships the current
		// (possibly stale) listing regardless; the NEXT Runtime GET will see
		// whatever the refresh produced. Waiting here would put MCP probing
		// back on the critical path to the model's first token — exactly the
		// cost this whole design removes (design doc §2.2.1; the "wait for a
		// fresh listing" requirement was considered and deliberately
		// dropped).
		//
		// The three conditions below (TTL expired / not cooling down / not
		// already probing) are only a cheap pre-filter to avoid spawning a
		// pointless goroutine on every request once a server is stuck
		// failing — they are NOT a lock claim. The authoritative single-
		// flight claim happens inside probeAndPersist's own MarkProbing call
		// (Task 7). Claiming MarkProbing here too would make that inner call
		// see the lock already held and silently return without doing any
		// work — the refresh would appear to fire but never actually run.
		//
		// Also skip when this server's own config failed to decrypt: the
		// probe would just fail predictably on empty credentials.
		listedAt, ttlSec, cooldownUntil, probeState := int64(0), int64(0), int64(0), ""
		if rt != nil {
			listedAt, ttlSec, cooldownUntil, probeState = rt.ListedAt, rt.TTLSec, rt.CooldownUntil, rt.ProbeState
		}
		if envErr == nil && hdrErr == nil &&
			listedAt+ttlSec < now && cooldownUntil < now && probeState != "probing" {
			// m is this loop's own *McpServer (freshly loaded from the DB
			// query above, not aliased by any other reference); env/headers
			// are plain, already-decrypted maps derived from it. None of the
			// three holds a live reference to c, c.Request(), or anything
			// else tied to this HTTP request's lifetime, which is required:
			// the request will have returned long before this goroutine
			// finishes (a stdio probe can take up to ~125s — see
			// probeAndPersist's timeout comment).
			go h.probeAndPersistAsync(m, env, headers)
		}

		servers = append(servers, rs)
	}

	approvalRows, err := h.svc.MCPApprovals().EffectiveApprovals(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	approvals := make([]approvalDTO, len(approvalRows))
	for i, a := range approvalRows {
		approvals[i] = approvalDTO{ServerID: a.ServerID, ToolName: a.ToolName}
	}

	// The one-time bootstrap ticket (tok, just resolved above) is consumed by
	// now — Resolve() deletes it unconditionally. Python holds no credential
	// for the rest of the run, but a user's "don't ask again" click on a
	// confirmation card happens mid-run. Mint a fresh, multi-use, run-scoped
	// write token in the SAME response so that write path needs no new
	// pipe (design doc §5.4): uid and tok are already the two identifiers
	// this handler resolved for its own auth check above, reused here as the
	// (user_id, session_id) pair the token is bound to.
	writeToken := h.runTokens.Mint(uid, tok)

	return c.JSON(http.StatusOK, map[string]any{
		"servers":     servers,
		"approvals":   approvals,
		"write_token": writeToken,
	})
}

// probeAndPersistAsync runs one TTL self-check refresh in the background on
// behalf of Runtime, which does not wait for it (see the TTL self-check
// comment above). It must NOT call MarkProbing itself — probeAndPersist
// claims that single-flight lock internally, and a second claim here would
// make the inner one fail and silently no-op instead of actually probing.
func (h *MCPHandler) probeAndPersistAsync(m *service.McpServer, env, headers map[string]string) {
	if _, err := h.probeAndPersist(m, env, headers); err != nil {
		log.Printf("mcp runtime self-check: background probe for server %d failed: %v", m.ID, err)
	}
}
