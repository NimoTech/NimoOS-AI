package service

import (
	"testing"
	"time"
)

const day = 24 * 60 * 60

func approve(t *testing.T, s *mcpApprovalService, id int64, tool, fp, sh string, seenAgo int64) {
	t.Helper()
	if err := s.Put(id, tool, fp, sh); err != nil {
		t.Fatalf("Put: %v", err)
	}
	now := time.Now().Unix()
	s.db.Exec(`UPDATE mcp_tool_approvals SET last_seen_at=? WHERE server_id=? AND tool_name=?`,
		now-seenAgo, id, tool)
}

// Regression test for rev 1 defect 1: none of these three operations may void any approval.
func TestToggleRenameNoteDoNotRevokeApprovals(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	rt := &mcpRuntimeService{db: db}
	id := seedServer(t, db)
	fp := IdentityFP("http", "https://x/mcp", "", nil, nil, map[string]string{"Authorization": "Bearer A"})
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: fp, TTLSec: 600},
		[]ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, `[]`)
	approve(t, ap, id, "create_issue", fp, "sh", 0)

	// Disable then re-enable (goes through UpdateMcpServer, which unconditionally refreshes updated_at).
	db.Exec(`UPDATE mcp_servers SET enabled=0, updated_at=? WHERE id=?`, time.Now().Unix()+1, id)
	db.Exec(`UPDATE mcp_servers SET enabled=1, updated_at=? WHERE id=?`, time.Now().Unix()+2, id)
	// Rename, edit note.
	db.Exec(`UPDATE mcp_servers SET name='renamed', note='n', updated_at=? WHERE id=?`, time.Now().Unix()+3, id)

	rows, err := ap.EffectiveApprovals("u")
	if err != nil {
		t.Fatalf("EffectiveApprovals: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("toggle/rename/note MUST NOT revoke approvals; got %d rows", len(rows))
	}
}

func TestConfigGateVoidsOnIdentityChange(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	rt := &mcpRuntimeService{db: db}
	id := seedServer(t, db)
	approve(t, ap, id, "create_issue", "OLD_FP", "sh", 0)
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: "NEW_FP", TTLSec: 600},
		[]ToolMeta{{Name: "create_issue", SchemaHash: "sh"}}, `[]`)

	rows, _ := ap.EffectiveApprovals("u")
	if len(rows) != 0 {
		t.Fatal("identity_fp change (URL/command/header-key-set) must void approvals")
	}
}

func TestInterfaceGateVoidsOnSchemaChange(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	rt := &mcpRuntimeService{db: db}
	id := seedServer(t, db)
	approve(t, ap, id, "create_issue", "FP", "OLD_SCHEMA", 0)
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: "FP", TTLSec: 600},
		[]ToolMeta{{Name: "create_issue", SchemaHash: "NEW_SCHEMA"}}, `[]`)

	if rows, _ := ap.EffectiveApprovals("u"); len(rows) != 0 {
		t.Fatal("schema_hash change must void the approval")
	}
}

func TestDescriptionChangeDoesNotVoid(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	rt := &mcpRuntimeService{db: db}
	id := seedServer(t, db)
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: "FP", TTLSec: 600},
		[]ToolMeta{{Name: "t", SchemaHash: "sh", DescHash: "d1"}}, `[]`)
	approve(t, ap, id, "t", "FP", "sh", 0)
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: "FP", TTLSec: 600},
		[]ToolMeta{{Name: "t", SchemaHash: "sh", DescHash: "d2"}}, `[]`)

	if rows, _ := ap.EffectiveApprovals("u"); len(rows) != 1 {
		t.Fatal("desc_hash MUST NOT participate in the gates — that was the rejected design")
	}
}

func TestStaleGateVoidsAfterSevenDays(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	rt := &mcpRuntimeService{db: db}
	id := seedServer(t, db)
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: "FP", TTLSec: 600},
		[]ToolMeta{{Name: "t", SchemaHash: "sh"}}, `[]`)
	approve(t, ap, id, "t", "FP", "sh", 8*day) // not seen for 8 days

	if rows, _ := ap.EffectiveApprovals("u"); len(rows) != 0 {
		t.Fatal("a tool unseen for >7d must be re-asked — it may be a different tool now")
	}
}

func TestWildcardSkipsInterfaceAndStaleGates(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	rt := &mcpRuntimeService{db: db}
	id := seedServer(t, db)
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: "FP", TTLSec: 600},
		[]ToolMeta{{Name: "t", SchemaHash: "sh"}}, `[]`)
	// '*' is not a real tool and never appears in tools_json; schema_hash is empty;
	// last_seen advances via successful probes.
	if err := ap.Put(id, "*", "FP", ""); err != nil {
		t.Fatalf("Put wildcard: %v", err)
	}
	rows, _ := ap.EffectiveApprovals("u")
	found := false
	for _, r := range rows {
		if r.ToolName == "*" {
			found = true
		}
	}
	if !found {
		t.Fatal("server-level '*' must skip the interface and stale gates, else it is voided instantly")
	}
}
