package v2

import (
	"encoding/json"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

// This file implements the four public authorization endpoints of design doc
// §8.1 — the settings UI's window into the approval store that Task 10 built
// and the identity card that Task 4/7 persist. It is deliberately separate
// from mcp.go's internal/loopback endpoints (Runtime, ApprovalsInternal,
// SchemasInternal): those are agent-facing and authenticated by run-scoped
// tickets/tokens; these are browser-facing and authenticated exactly like
// the existing public /mcp/servers* handlers in mcp.go (h.userID, reading
// X-NimoOS-User-ID — set by route/v2.go's JWT middleware after verifying the
// caller's bearer token). See ApprovalsInternal's doc comment (mcp.go) for
// the security model these endpoints mirror; the difference in auth scheme
// is deliberate, not an oversight — do not port the write-token scheme here.

// toolStateDTO is one element of GET .../tools' "tools" array. Every field
// comes from already-persisted rows (mcp_server_runtime.tools_json +
// mcp_tool_approvals) — computing this response makes ZERO network calls,
// which is the whole payoff of persisting the identity card (before this
// design, showing the tool list required a live connection to the server).
// StaleReason is populated ONLY for a currently-void approval (identical to
// ListForServer's semantics) and is empty otherwise; the config gate in
// particular (server identity changed) is not derivable client-side — the
// browser never sees identity_fp — so without this field an edited server's
// stale approvals would render as ordinary "approved" toggles, silently
// telling the user they won't be re-prompted when they will be.
// StaleReasonKey is StaleReason's machine-readable counterpart (one of
// service.StaleReasonXxx) — added so the UI can map it through its own
// error_key-style i18n table (mcpErrorKey.ts's existing pattern for
// testMCPServer) instead of rendering the English prose straight to screen.
// StaleReason is kept as-is alongside it: nothing that reads it today breaks.
type toolStateDTO struct {
	Name           string `json:"name"`
	Approved       bool   `json:"approved"`
	LastSeenAt     int64  `json:"last_seen_at"`
	DescChanged    bool   `json:"desc_changed"`
	StaleReason    string `json:"stale_reason,omitempty"`
	StaleReasonKey string `json:"stale_reason_key,omitempty"`
}

// toolsResponseDTO is the full body of GET .../tools. ServerLevelApproved
// reports whether a server-level ('*') approval row exists for this server —
// true even if it is currently void, mirroring each toolStateDTO row's own
// Approved semantics (see its doc comment above): the settings UI shows the
// switch on with an explanation rather than silently off, so a void grant
// still tells the user what they once approved. Without this field the UI
// has no way to know a wildcard grant exists at all (byName["*"] is server-
// side only), so its server-level toggle always initialized off even when a
// live grant was in force.
//
// TotalStoredApprovals (mcp-progressive-disclosure Task 21 fix round) is the
// raw count of EVERY row ListForServer returned for this server — i.e.
// len(approvals) below, before the Tools loop filters it down to whatever
// currently appears in metas. This is the number a caller needs for "how many
// approvals will CASCADE delete along with this server", and it can exceed
// the count implied by the Tools/ServerLevelApproved fields above: a tool
// that has since been removed from the server's live tools_json snapshot
// never gets a toolStateDTO row at all (the loop below only ranges over
// metas), so its still-stored approval would otherwise be invisible to any
// caller trying to derive a count from Tools/ServerLevelApproved alone. No
// new query — approvals is already fetched below for the per-tool lookup;
// this just reports its length instead of discarding it.
type toolsResponseDTO struct {
	Tools                     []toolStateDTO `json:"tools"`
	ServerLevelApproved       bool           `json:"server_level_approved"`
	ServerLevelStaleReason    string         `json:"server_level_stale_reason,omitempty"`
	ServerLevelStaleReasonKey string         `json:"server_level_stale_reason_key,omitempty"`
	TotalStoredApprovals      int            `json:"total_stored_approvals"`
}

// approvalSummaryDTO is one element of GET /mcp/approvals' cross-server
// summary. ServerHandle is required — Task 21 groups the summary page by
// server.
type approvalSummaryDTO struct {
	ServerID     int64  `json:"server_id"`
	ServerHandle string `json:"server_handle"`
	ToolName     string `json:"tool_name"`
}

// putApprovalRequest is the body for PUT .../approvals/:tool. It carries
// ONLY the user's yes/no decision. identity_fp/schema_hash/desc_hash are
// deliberately not fields here (mirroring mcpApprovalsRequest in mcp.go) —
// see PutApproval's doc comment for why they must never come from the
// request.
type putApprovalRequest struct {
	Approved bool `json:"approved"`
}

// lookupToolMeta returns the schema_hash and desc_hash currently recorded
// for toolName in a runtime row's ToolsJSON (a JSON array of {name,
// schema_hash, desc_hash}). Both come back "" when the tool is not (or no
// longer) present in the listing — this is the intended, safe outcome, not
// an error: Task 10's interface gate treats an empty stored schema_hash as a
// failed gate, and an empty stored desc_hash reports "description changed"
// as false, so the approval this feeds into simply stays inert on the gate
// (or quiet on the badge) until the tool reappears in a real listing.
// Neither hash is computed here — both are Python-only values
// (agent/mcp_client/hashing.py), only looked up. Shared by both writers of
// ordinary tool approvals: ApprovalsInternal's confirm-card path (mcp.go)
// and PutApproval below.
func lookupToolMeta(toolsJSON, toolName string) (schemaHash, descHash string) {
	var tools []service.ToolMeta
	_ = json.Unmarshal([]byte(toolsJSON), &tools)
	for _, tl := range tools {
		if tl.Name == toolName {
			return tl.SchemaHash, tl.DescHash
		}
	}
	return "", ""
}

// ownerOrForbidden resolves the caller's user id the same way the existing
// public /mcp/servers* handlers do (h.userID) and checks ownership of :id
// via GetMcpServer(id, uid). Mirrors ApprovalsInternal's ownership check
// (mcp.go): GetMcpServer filters by (id, user_id) together, so a server_id
// that exists but belongs to someone else, and one that does not exist at
// all, both come back as an error here and both produce the SAME 403 below —
// deliberately indistinguishable, or the endpoint would be an enumeration
// oracle over other users' server ids.
func (h *MCPHandler) ownerOrForbidden(c echo.Context) (uid string, id int64, err error) {
	uid, err = h.userID(c)
	if err != nil {
		return "", 0, err
	}
	id, err = strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return "", 0, echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	if _, err := h.svc.MCP().GetMcpServer(id, uid); err != nil {
		return "", 0, echo.NewHTTPError(http.StatusForbidden, "server does not belong to the authenticated user")
	}
	return uid, id, nil
}

// Tools handles GET /v1/ai/mcp/servers/:id/tools (design doc §8.1). It reads
// the tool list straight out of mcp_server_runtime.tools_json and layers
// each tool's approval state, last_seen_at, desc_changed badge and
// stale_reason on top, using only mcp_tool_approvals — it never dials the
// MCP server itself.
func (h *MCPHandler) Tools(c echo.Context) error {
	_, id, err := h.ownerOrForbidden(c)
	if err != nil {
		return err
	}

	// A server with no runtime row yet has simply never been probed — a
	// normal state (Task 4), not an error. Report an empty tool list rather
	// than failing.
	rt, err := h.svc.MCPRuntime().Get(id)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	if rt == nil {
		return c.JSON(http.StatusOK, toolsResponseDTO{Tools: []toolStateDTO{}})
	}

	var metas []service.ToolMeta
	_ = json.Unmarshal([]byte(rt.ToolsJSON), &metas)

	approvals, err := h.svc.MCPApprovals().ListForServer(id)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	byName := make(map[string]service.ApprovalRow, len(approvals))
	for _, a := range approvals {
		byName[a.ToolName] = a
	}
	wildcard, hasWildcard := byName["*"]

	out := make([]toolStateDTO, 0, len(metas))
	for _, m := range metas {
		dto := toolStateDTO{Name: m.Name}
		switch row, ok := byName[m.Name]; {
		case ok:
			// An explicit per-tool approval is the more specific record;
			// prefer it over a server-level one for
			// last_seen_at/desc_changed/stale_reason(_key).
			dto.Approved = true
			dto.LastSeenAt = row.LastSeenAt
			dto.StaleReason = row.StaleReason
			dto.StaleReasonKey = row.StaleReasonKey
			// desc_changed: stored desc_hash non-empty AND different from the
			// current one. An empty stored value means "approved before we
			// started recording it" — report false, not true, or every
			// pre-existing approval would light up the badge on upgrade.
			dto.DescChanged = row.DescHash != "" && row.DescHash != m.DescHash
		case hasWildcard:
			// Server-level '*' approval covers this tool too (§5.1.1). It
			// never carries a desc_hash of its own (a wildcard is not a real
			// tool and has no description), so it never lights the badge.
			dto.Approved = true
			dto.LastSeenAt = wildcard.LastSeenAt
			dto.StaleReason = wildcard.StaleReason
			dto.StaleReasonKey = wildcard.StaleReasonKey
		}
		out = append(out, dto)
	}

	resp := toolsResponseDTO{Tools: out, TotalStoredApprovals: len(approvals)}
	if hasWildcard {
		// True even if void (see toolsResponseDTO's doc comment) — mirrors
		// each toolStateDTO row's own Approved semantics above.
		resp.ServerLevelApproved = true
		resp.ServerLevelStaleReason = wildcard.StaleReason
		resp.ServerLevelStaleReasonKey = wildcard.StaleReasonKey
	}
	return c.JSON(http.StatusOK, resp)
}

// PutApproval handles PUT /v1/ai/mcp/servers/:id/approvals/:tool. It is the
// browser-facing counterpart to ApprovalsInternal (mcp.go) and shares its
// core security property:
//
//  1. Ownership before anything else (ownerOrForbidden).
//  2. identity_fp, schema_hash and desc_hash are ALWAYS read from the
//     server's CURRENT mcp_server_runtime row, never from the request body —
//     the body carries only {"approved": bool}. A caller able to supply
//     these directly could forge an approval whose stored fingerprint always
//     matches itself, defeating the config/interface gates in
//     EffectiveApprovals (which compare the stored value against the
//     CURRENT runtime observation).
//  3. tool_name=="*" routes to PutServerLevel (grant) / Delete(id,"*")
//     (revoke); every other name goes to Put / Delete. Put rejects "*"
//     outright (Task 10) — do not try to force it through.
//
// :tool is percent-decoded (url.PathUnescape) before use, matching this
// codebase's convention for path segments that name an entity rather than
// an opaque id (see models.go's DeleteModel): without it, a client that
// strictly encodes the wildcard as "%2A" would silently write a junk
// approval literally named "%2A" instead of granting server-level consent.
func (h *MCPHandler) PutApproval(c echo.Context) error {
	_, id, err := h.ownerOrForbidden(c)
	if err != nil {
		return err
	}
	toolName, err := url.PathUnescape(c.Param("tool"))
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid tool encoding")
	}
	toolName = strings.TrimSpace(toolName)
	if toolName == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "tool name required")
	}

	var req putApprovalRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}

	if !req.Approved {
		if toolName == "*" {
			err = h.svc.MCPApprovals().Delete(id, "*")
		} else {
			err = h.svc.MCPApprovals().Delete(id, toolName)
		}
		if err != nil {
			return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
		}
		return c.NoContent(http.StatusNoContent)
	}

	// rt is nil when this server has never been probed (Task 4) — a normal
	// state, not an error. identityFP/schemaHash/descHash then stay "",
	// which is safe: EffectiveApprovals' gates fail closed on an empty
	// stored value, and an empty stored desc_hash reports desc_changed=false
	// rather than true.
	rt, err := h.svc.MCPRuntime().Get(id)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	var identityFP, schemaHash, descHash string
	if rt != nil {
		identityFP = rt.IdentityFP
		if toolName != "*" {
			schemaHash, descHash = lookupToolMeta(rt.ToolsJSON, toolName)
		}
	}

	if toolName == "*" {
		// PutServerLevel is the only path allowed to write the '*' sentinel
		// row; Put rejects tool_name=="*" (Task 10).
		err = h.svc.MCPApprovals().PutServerLevel(id, identityFP)
	} else {
		err = h.svc.MCPApprovals().Put(id, toolName, identityFP, schemaHash, descHash)
	}
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

// DeleteApprovals handles DELETE /v1/ai/mcp/servers/:id/approvals — revoke
// every approval (including the server-level '*' row) for one server, e.g.
// the settings page's "revoke all" action or a delete-server confirmation
// (design doc §8.2 point 6).
func (h *MCPHandler) DeleteApprovals(c echo.Context) error {
	_, id, err := h.ownerOrForbidden(c)
	if err != nil {
		return err
	}
	if err := h.svc.MCPApprovals().DeleteAll(id); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

// ListApprovals handles GET /v1/ai/mcp/approvals — a cross-server summary of
// the caller's currently effective approvals (the same gated set
// EffectiveApprovals hands the agent at run start), for the settings page's
// "approved tools" overview (design doc §8.2 point 5). server_handle is
// required — Task 21 groups the page by server.
func (h *MCPHandler) ListApprovals(c echo.Context) error {
	uid, err := h.userID(c)
	if err != nil {
		return err
	}
	rows, err := h.svc.MCPApprovals().EffectiveApprovals(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	// One query for every server's runtime row, keyed by server_id, instead
	// of a Get() per distinct server_id in the loop below.
	runtimes, err := h.svc.MCPRuntime().List(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}

	out := make([]approvalSummaryDTO, len(rows))
	for i, r := range rows {
		handle := ""
		if rt := runtimes[r.ServerID]; rt != nil {
			handle = rt.Handle
		}
		out[i] = approvalSummaryDTO{ServerID: r.ServerID, ServerHandle: handle, ToolName: r.ToolName}
	}
	return c.JSON(http.StatusOK, map[string]any{"items": out})
}
