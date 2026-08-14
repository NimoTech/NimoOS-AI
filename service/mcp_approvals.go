package service

import (
	"database/sql"
	"encoding/json"
	"time"
)

// staleWindowSec is the "gone dark" gate: a tool not seen in a listing for
// this long must be re-approved, because a same-named tool that reappears
// after a long absence may not be the same tool.
const staleWindowSec = 7 * 24 * 60 * 60

// hygieneWindowSec is pure cleanup, not a security boundary: rows unseen for
// this long are deleted outright rather than merely gated.
const hygieneWindowSec = 90 * 24 * 60 * 60

// ApprovalRow is one row of the tool-approval store, as read back by either
// the gated view (EffectiveApprovals) or the raw view (ListForServer).
// StaleReason is populated only by ListForServer, for the settings UI to
// explain to the user why a listed approval is currently void; it is
// display/diagnostic only and never feeds any pass/fail decision.
type ApprovalRow struct {
	ServerID    int64
	ToolName    string
	StaleReason string
}

type mcpApprovalService struct{ db *sql.DB }

// Put records (or refreshes) one tool approval. approved_at and last_seen_at
// are both set to now: approved_at marks when the user consented,
// last_seen_at is reset here so a freshly (re-)approved tool doesn't
// immediately fail the stale gate.
func (s *mcpApprovalService) Put(serverID int64, toolName, identityFP, schemaHash string) error {
	now := time.Now().Unix()
	_, err := s.db.Exec(`
		INSERT INTO mcp_tool_approvals (server_id, tool_name, identity_fp, schema_hash, approved_at, last_seen_at)
		VALUES (?, ?, ?, ?, ?, ?)
		ON CONFLICT(server_id, tool_name) DO UPDATE SET
			identity_fp=excluded.identity_fp, schema_hash=excluded.schema_hash,
			approved_at=excluded.approved_at, last_seen_at=excluded.last_seen_at`,
		serverID, toolName, identityFP, schemaHash, now, now)
	return err
}

// Delete revokes one tool's approval.
func (s *mcpApprovalService) Delete(serverID int64, toolName string) error {
	_, err := s.db.Exec(`DELETE FROM mcp_tool_approvals WHERE server_id=? AND tool_name=?`, serverID, toolName)
	return err
}

// DeleteAll revokes every approval (including the server-level '*' row) for
// one server, e.g. when the user removes the server or resets its consent.
func (s *mcpApprovalService) DeleteAll(serverID int64) error {
	_, err := s.db.Exec(`DELETE FROM mcp_tool_approvals WHERE server_id=?`, serverID)
	return err
}

// EffectiveApprovals returns the set of approvals that have passed all four
// invalidation gates. The gates run in Go, after a single JOIN query (design
// doc §5.2): all four gates' inputs are gathered here in one pass, so the
// Python side receives a ready-to-use set and _ensure_confirmed degrades to
// a single in-memory lookup.
//
// The four gates:
//
//	Config    — void if identity_fp no longer matches. The judgement is
//	            identity_fp, NOT mcp_servers.updated_at: UpdateMcpServer
//	            bumps updated_at unconditionally, so keying on it would void
//	            every approval whenever the user disables/re-enables a
//	            server, renames it, or edits its note — none of which change
//	            what the server actually is.
//	Interface — void if schema_hash differs from the one currently in the
//	            server's tools_json for that tool. The tool's arguments
//	            changed, so what the user consented to is not what would run.
//	Stale     — void if last_seen_at is older than 7 days. A tool that
//	            vanished for a week and came back under the same name may not
//	            be the same tool.
//	Hygiene   — rows unseen for 90 days are deleted outright. This is
//	            cleanup, not a security mechanism.
//
// tool_name='*' is the server-level approval. It is not a real tool — it
// never appears in tools_json, so it has no schema_hash of its own, and its
// last_seen_at is advanced by any successful non-empty probe rather than by
// its own presence in a listing — so it skips the interface and stale gates.
//
// A server with no runtime row at all (never successfully probed) has no
// entry in the runtime map below, so every non-wildcard approval for it
// fails the config gate (identity_fp compares "" from the DB default against
// whatever was approved) and is excluded. This is the safe default: without
// a probe there is nothing to compare against, so the tool is treated as
// unconfirmed rather than silently trusted.
func (s *mcpApprovalService) EffectiveApprovals(userID string) ([]ApprovalRow, error) {
	// Gate 4 (hygiene) runs first and unconditionally: delete rows that
	// haven't been seen in 90 days. last_seen_at > 0 excludes rows that have
	// never been heartbeated at all (freshly approved, probe pending) —
	// without that guard a brand-new approval with last_seen_at=0 would be
	// deleted immediately.
	now := time.Now().Unix()
	if _, err := s.db.Exec(
		`DELETE FROM mcp_tool_approvals WHERE last_seen_at > 0 AND last_seen_at < ?`,
		now-hygieneWindowSec); err != nil {
		return nil, err
	}

	rows, err := s.db.Query(`
		SELECT a.server_id, a.tool_name, a.identity_fp, a.schema_hash, a.last_seen_at,
			r.identity_fp, r.tools_json
		FROM mcp_tool_approvals a
		JOIN mcp_servers s ON s.id = a.server_id
		LEFT JOIN mcp_server_runtime r ON r.server_id = a.server_id
		WHERE s.user_id = ?`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	type toolSet map[string]ToolMeta
	toolSetCache := map[int64]toolSet{}

	var out []ApprovalRow
	for rows.Next() {
		var serverID int64
		var toolName, approvalFP, approvalSchema string
		var lastSeenAt int64
		var runtimeFP sql.NullString
		var toolsJSON sql.NullString
		if err := rows.Scan(&serverID, &toolName, &approvalFP, &approvalSchema, &lastSeenAt,
			&runtimeFP, &toolsJSON); err != nil {
			return nil, err
		}

		// Config gate: identity_fp must still match the current runtime
		// observation. A server with no runtime row at all yields runtimeFP
		// as NULL/"" here, which never equals a real approved fingerprint,
		// so it correctly fails closed rather than being treated as trusted.
		if approvalFP != runtimeFP.String {
			continue
		}

		if toolName == "*" {
			// Server-level approval: skips the interface and stale gates,
			// since '*' never appears in tools_json and its last_seen_at is
			// driven by probe success, not by its own listing membership.
			out = append(out, ApprovalRow{ServerID: serverID, ToolName: toolName})
			continue
		}

		// Interface gate: schema_hash must match the tool's current entry
		// in tools_json. Parse tools_json into a map once per server and
		// reuse it across rows, rather than re-parsing per row.
		tools, ok := toolSetCache[serverID]
		if !ok {
			tools = toolSet{}
			if toolsJSON.Valid && toolsJSON.String != "" {
				var metas []ToolMeta
				if err := json.Unmarshal([]byte(toolsJSON.String), &metas); err == nil {
					for _, m := range metas {
						tools[m.Name] = m
					}
				}
			}
			toolSetCache[serverID] = tools
		}
		meta, present := tools[toolName]
		if !present || meta.SchemaHash != approvalSchema {
			continue
		}

		// Stale gate: last_seen_at must be within the last 7 days.
		if now-lastSeenAt > staleWindowSec {
			continue
		}

		out = append(out, ApprovalRow{ServerID: serverID, ToolName: toolName})
	}
	return out, rows.Err()
}

// ListForServer returns every approval row for a server WITHOUT running the
// gates, for the settings UI: it must show the user everything they
// approved, even approvals that are currently void, with StaleReason
// explaining why. StaleReason is display/diagnostic only; it never feeds a
// pass/fail decision — EffectiveApprovals owns that.
func (s *mcpApprovalService) ListForServer(serverID int64) ([]ApprovalRow, error) {
	rows, err := s.db.Query(`
		SELECT a.tool_name, a.identity_fp, a.schema_hash, a.last_seen_at,
			r.identity_fp, r.tools_json
		FROM mcp_tool_approvals a
		LEFT JOIN mcp_server_runtime r ON r.server_id = a.server_id
		WHERE a.server_id = ?`, serverID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	now := time.Now().Unix()
	var tools map[string]ToolMeta
	var toolsLoaded bool

	var out []ApprovalRow
	for rows.Next() {
		var toolName, approvalFP, approvalSchema string
		var lastSeenAt int64
		var runtimeFP sql.NullString
		var toolsJSON sql.NullString
		if err := rows.Scan(&toolName, &approvalFP, &approvalSchema, &lastSeenAt,
			&runtimeFP, &toolsJSON); err != nil {
			return nil, err
		}

		reason := ""
		switch {
		case approvalFP != runtimeFP.String:
			reason = "config changed: server identity no longer matches the approved one"
		case toolName == "*":
			// Server-level approval: no interface/stale reasons apply.
		default:
			if !toolsLoaded {
				tools = map[string]ToolMeta{}
				if toolsJSON.Valid && toolsJSON.String != "" {
					var metas []ToolMeta
					if err := json.Unmarshal([]byte(toolsJSON.String), &metas); err == nil {
						for _, m := range metas {
							tools[m.Name] = m
						}
					}
				}
				toolsLoaded = true
			}
			meta, present := tools[toolName]
			switch {
			case !present:
				reason = "tool no longer offered by the server"
			case meta.SchemaHash != approvalSchema:
				reason = "interface changed: tool's schema no longer matches the approved one"
			case now-lastSeenAt > staleWindowSec:
				reason = "stale: tool not seen in the last 7 days"
			}
		}

		out = append(out, ApprovalRow{ServerID: serverID, ToolName: toolName, StaleReason: reason})
	}
	return out, rows.Err()
}
