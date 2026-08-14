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

// --- internal loopback write-back endpoints ---
//
// These three endpoints are the ONLY way the Python agent writes anything
// back into Go for MCP. Everything else the agent does with MCP data is
// read-only (Runtime, the schemas fetch below). ApprovalsInternal in
// particular is a privilege boundary, not plumbing: it records that a user
// consented to a tool, which suppresses future confirmation prompts for
// that (server, tool) pair.

// mcpApprovalsRequest is the body for POST /_internal/mcp/approvals. Only
// server_id and tool_name are caller-supplied — Python derives both from the
// confirm_id it minted when it showed the confirmation card, never from the
// browser. identity_fp and schema_hash are deliberately NOT fields here: see
// ApprovalsInternal for why they must never come from the request.
type mcpApprovalsRequest struct {
	ServerID int64  `json:"server_id"`
	ToolName string `json:"tool_name"`
}

// lookupSchemaHash returns the schema_hash currently recorded for toolName
// in a McpServerRuntime's ToolsJSON (a JSON array of {name, schema_hash,
// desc_hash}). It returns "" if the tool is not present in the listing —
// this is the intended, safe outcome, not an error: Task 10's interface gate
// treats an empty stored schema_hash as a failed gate, so the approval this
// value feeds into simply will not take effect until the tool actually
// appears in a listing. schema_hash itself is computed only in Python; this
// function only looks up an already-computed value, never derives one.
func lookupSchemaHash(toolsJSON, toolName string) string {
	var tools []service.ToolMeta
	_ = json.Unmarshal([]byte(toolsJSON), &tools)
	for _, tl := range tools {
		if tl.Name == toolName {
			return tl.SchemaHash
		}
	}
	return ""
}

// resolveWriteToken reads X-Agent-MCP-Write-Token and resolves it against
// h.runTokens. On failure (missing, unknown, or expired token) it writes the
// 401 directly via c.JSON (not echo.NewHTTPError) so that a caller
// inspecting rec.Code directly — as this handler's own tests do, and as
// Runtime's ticket check elsewhere in this file already establishes the
// convention for — sees the right status even without Echo's error handler
// in the loop, and returns ok=false with that response already written as
// handlerErr so the caller can just `return handlerErr` immediately. Shared
// by every endpoint gated on this run-scoped credential (ApprovalsInternal,
// SchemasInternal) so the 401 path is defined in exactly one place.
func (h *MCPHandler) resolveWriteToken(c echo.Context) (uid string, ok bool, handlerErr error) {
	tok := c.Request().Header.Get("X-Agent-MCP-Write-Token")
	uid, ok = h.runTokens.Resolve(tok)
	if !ok {
		return "", false, c.JSON(http.StatusUnauthorized, map[string]string{"message": "invalid or missing mcp write token"})
	}
	return uid, true, nil
}

// ApprovalsInternal handles POST /v1/ai/_internal/mcp/approvals — Python's
// only write path into Go for MCP (design doc §5.4). It records that the
// user consented ("don't ask again") to server_id+tool_name, which later
// suppresses confirmation prompts for that pair via
// MCPApprovals().EffectiveApprovals.
//
// Security-sensitive, read carefully before changing:
//
//  1. Auth is the run-scoped write token (X-Agent-MCP-Write-Token), minted
//     once per run by Runtime() and resolved via resolveWriteToken. No
//     token, an unknown token, or an expired one is a 401. The authorization
//     subject must never be inferable from an unauthenticated call.
//  2. The token resolves to a user_id. The target server has an owner;
//     if they differ this is a 403 — otherwise any run could grant
//     approvals on a server it does not own. GetMcpServer filters by
//     (id, user_id) together, so a server_id that exists but belongs to
//     someone else comes back as sql.ErrNoRows here, indistinguishable
//     from "does not exist" — both cases are correctly rejected the same
//     way, since a caller with neither ownership nor existence gets no
//     information either way.
//  3. identity_fp and schema_hash are ALWAYS read from the server's CURRENT
//     mcp_server_runtime row, never from the request body (the request
//     struct above has no fields for them at all). If a caller could supply
//     these directly, it could mint an approval whose stored fingerprint
//     always matches itself — the config/interface gates in
//     EffectiveApprovals compare the stored value against the CURRENT
//     runtime observation, so a self-consistent forged pair would never be
//     invalidated, producing an approval that can never expire.
//  4. tool_name is trimmed exactly once (into the toolName local) and that
//     same trimmed value is used for the emptiness check, the "*" routing
//     decision, and the write itself. Validating against the trimmed value
//     while writing the untrimmed one would let " create_issue " pass
//     validation but land in storage as literally " create_issue " — a
//     value no gate could ever match, silently producing an approval that
//     re-asks forever with no error surfaced anywhere. (A padded wildcard
//     like " * " already fails safe under this scheme too: it is not equal
//     to "*", so it falls through to Put as an ordinary, never-matching tool
//     name instead of ever reaching PutServerLevel.)
func (h *MCPHandler) ApprovalsInternal(c echo.Context) error {
	uid, ok, err := h.resolveWriteToken(c)
	if !ok {
		return err
	}

	var req mcpApprovalsRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	toolName := strings.TrimSpace(req.ToolName)
	if req.ServerID == 0 || toolName == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "server_id and tool_name required")
	}

	if _, err := h.svc.MCP().GetMcpServer(req.ServerID, uid); err != nil {
		return echo.NewHTTPError(http.StatusForbidden, "server does not belong to the authenticated user")
	}

	// rt is nil when this server has never had a successful probe (Task 4) —
	// a normal state, not an error. identityFP/schemaHash then stay "",
	// which is safe: EffectiveApprovals' config/interface gates both fail
	// closed on an empty stored value, so the write below is accepted but
	// inert until a real listing exists.
	rt, err := h.svc.MCPRuntime().Get(req.ServerID)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	var identityFP string
	if rt != nil {
		identityFP = rt.IdentityFP
	}

	if toolName == "*" {
		// PutServerLevel is the only path allowed to write the '*' sentinel
		// row; Put itself rejects tool_name=="*" (Task 10).
		err = h.svc.MCPApprovals().PutServerLevel(req.ServerID, identityFP)
	} else {
		var schemaHash string
		if rt != nil {
			schemaHash = lookupSchemaHash(rt.ToolsJSON, toolName)
		}
		err = h.svc.MCPApprovals().Put(req.ServerID, toolName, identityFP, schemaHash)
	}
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

// SchemasInternal handles GET /v1/ai/_internal/mcp/servers/:id/schemas — the
// L2 loopback fetch (design doc §1.2.1/§2.2.1): a millisecond, no-third-
// party-network read that expands one server's full tool schemas the first
// time a run needs them. listed_at MUST ship alongside the schema bodies:
// Python's in-memory cache is keyed on it, and without it a changed tool
// description could never invalidate that cache and reach the model.
//
// This endpoint is read-only — it never writes runtime rows or schemas; Go
// is the sole writer of that state, populated by the probe path (Task 7),
// not by anything reachable from here.
//
// Auth: this requires the same run-scoped write token as ApprovalsInternal,
// plus an ownership check against the resolved user_id. LocalhostOnly alone
// is NOT sufficient here — :id is a bare, sequential primary key, so without
// a positive credential and ownership check, any local process could walk
// id=1,2,3... and read every user's MCP tool names, argument schemas and
// third-party prose (and even derive a probed/exists oracle from
// listed_at != 0). Every other user-scoped internal endpoint in this service
// carries a positive credential on top of LocalhostOnly (Runtime requires
// the MCP ticket; ProviderCredentials requires a constant-time token
// compare) — this endpoint follows the same doctrine.
func (h *MCPHandler) SchemasInternal(c echo.Context) error {
	uid, ok, err := h.resolveWriteToken(c)
	if !ok {
		return err
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	if _, err := h.svc.MCP().GetMcpServer(id, uid); err != nil {
		return echo.NewHTTPError(http.StatusForbidden, "server does not belong to the authenticated user")
	}
	listedAt, schemasJSON, err := h.svc.MCPRuntime().GetSchemas(id)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	var schemas []any
	_ = json.Unmarshal([]byte(schemasJSON), &schemas)
	if schemas == nil {
		schemas = []any{}
	}
	return c.JSON(http.StatusOK, map[string]any{
		"listed_at": listedAt,
		"schemas":   schemas,
	})
}

// ReleaseTokenInternal handles POST /v1/ai/_internal/mcp/token/release. The
// agent calls this at run teardown to shrink its write token's replay window
// back down to the run's actual duration, instead of leaving it valid for
// the full 24h backstop (see RunTokenStore's doc comment for why 24h exists
// at all). The token itself is the sole key Release needs; a missing or
// already-invalid token is a harmless no-op, since releasing is idempotent
// cleanup that a caller should never have to fail-check.
func (h *MCPHandler) ReleaseTokenInternal(c echo.Context) error {
	tok := c.Request().Header.Get("X-Agent-MCP-Write-Token")
	if tok != "" {
		h.runTokens.Release(tok)
	}
	return c.NoContent(http.StatusNoContent)
}
