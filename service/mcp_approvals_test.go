package service

import (
	"testing"
	"time"
)

const day = 24 * 60 * 60

// approve writes one approval row and then back-dates its last_seen_at by
// seenAgo seconds. tool == "*" goes through PutServerLevel (Put itself
// rejects "*" — see its doc comment) so this helper works for both ordinary
// tools and the server-level approval.
func approve(t *testing.T, s *mcpApprovalService, id int64, tool, fp, sh string, seenAgo int64) {
	t.Helper()
	var err error
	if tool == "*" {
		err = s.PutServerLevel(id, fp)
	} else {
		err = s.Put(id, tool, fp, sh)
	}
	if err != nil {
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
	// '*' is not a real tool and never appears in tools_json; schema_hash is empty.
	// Back-date last_seen_at by 8 days (past the 7-day stale window) so this test
	// actually exercises the stale-gate skip, not just the interface-gate skip —
	// a freshly-approved row's last_seen_at would never have been old enough to
	// tell the two apart.
	approve(t, ap, id, "*", "FP", "", 8*day)
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

// TestWildcardConfigGateVoidsOnIdentityChange guards against a tempting but
// wrong refactor: moving the tool_name=="*" short-circuit BEFORE the config
// gate. The wildcard skips the interface and stale gates ONLY — it must
// still die on an identity change like any other approval.
func TestWildcardConfigGateVoidsOnIdentityChange(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	rt := &mcpRuntimeService{db: db}
	id := seedServer(t, db)
	if err := ap.PutServerLevel(id, "OLD_FP"); err != nil {
		t.Fatalf("PutServerLevel: %v", err)
	}
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: "NEW_FP", TTLSec: 600},
		[]ToolMeta{{Name: "t", SchemaHash: "sh"}}, `[]`)

	if rows, _ := ap.EffectiveApprovals("u"); len(rows) != 0 {
		t.Fatal("the server-level '*' approval must still fail the config gate on identity change")
	}
}

// TestPutRejectsWildcardToolName is the regression test for the '*'-namespace
// hole: Put must never be able to write the server-level sentinel row, no
// matter what name is passed to it — that is PutServerLevel's job alone.
func TestPutRejectsWildcardToolName(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	if err := ap.Put(seedServer(t, db), "*", "FP", "sh"); err == nil {
		t.Fatal("Put must reject tool_name '*' — use PutServerLevel for the server-level approval")
	}
}

// TestNoRuntimeRowFailsClosedForOrdinaryTool locks in the safe default for a
// server that has never been successfully probed: an approval for it must
// never appear in EffectiveApprovals, because there is nothing to compare
// its identity_fp/schema_hash against.
func TestNoRuntimeRowFailsClosedForOrdinaryTool(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	id := seedServer(t, db)
	if err := ap.Put(id, "create_issue", "FP", "sh"); err != nil {
		t.Fatalf("Put: %v", err)
	}
	// Deliberately no SaveSuccess: no runtime row exists for this server.

	if rows, _ := ap.EffectiveApprovals("u"); len(rows) != 0 {
		t.Fatal("an approval for a server with no runtime row must fail closed, not be trusted")
	}
}

// TestWildcardWithEmptyIdentityFPFailsClosedWithNoRuntime is the regression
// test for the config-gate fail-open hole: an empty approval identity_fp
// compared against an empty (absent) runtime identity_fp must NOT be treated
// as a match. Before the fix, this produced a wildcard approval that passed
// the config gate forever on a server that had never successfully probed.
func TestWildcardWithEmptyIdentityFPFailsClosedWithNoRuntime(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	id := seedServer(t, db)
	if err := ap.PutServerLevel(id, ""); err != nil {
		t.Fatalf("PutServerLevel: %v", err)
	}
	// Deliberately no SaveSuccess: no runtime row exists for this server, so
	// its identity_fp reads back as "" — the same as the approval's.

	if rows, _ := ap.EffectiveApprovals("u"); len(rows) != 0 {
		t.Fatal("an empty identity_fp must never be treated as matching an absent runtime fingerprint")
	}
}

// TestInterfaceGateFailsClosedWhenBothSchemaHashesEmpty is the regression
// test for the interface-gate fail-open hole: an approval with no recorded
// schema_hash compared against a tools_json entry with no schema_hash key
// must NOT be treated as a match, or the tool's arguments could change
// freely without ever voiding the approval.
func TestInterfaceGateFailsClosedWhenBothSchemaHashesEmpty(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	rt := &mcpRuntimeService{db: db}
	id := seedServer(t, db)
	if err := ap.Put(id, "t", "FP", ""); err != nil {
		t.Fatalf("Put: %v", err)
	}
	rt.SaveSuccess(&McpServerRuntime{ServerID: id, IdentityFP: "FP", TTLSec: 600},
		[]ToolMeta{{Name: "t", SchemaHash: ""}}, `[]`)

	if rows, _ := ap.EffectiveApprovals("u"); len(rows) != 0 {
		t.Fatal("two empty schema_hash values must never be treated as matching")
	}
}

// TestHygieneKeepsNeverHeartbeatedRow locks in the last_seen_at > 0 guard on
// the hygiene DELETE: a freshly-approved row that has never been
// heartbeated (last_seen_at == 0, e.g. probe still pending) must survive,
// not be deleted on sight.
func TestHygieneKeepsNeverHeartbeatedRow(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	id := seedServer(t, db)
	if err := ap.Put(id, "t", "FP", "sh"); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if _, err := db.Exec(`UPDATE mcp_tool_approvals SET last_seen_at=0 WHERE server_id=? AND tool_name='t'`, id); err != nil {
		t.Fatalf("pin last_seen_at: %v", err)
	}

	if _, err := ap.EffectiveApprovals("u"); err != nil {
		t.Fatalf("EffectiveApprovals: %v", err)
	}

	var n int
	if err := db.QueryRow(`SELECT count(*) FROM mcp_tool_approvals WHERE server_id=? AND tool_name='t'`, id).Scan(&n); err != nil {
		t.Fatalf("count: %v", err)
	}
	if n != 1 {
		t.Fatal("a never-heartbeated row (last_seen_at=0) must survive the hygiene sweep")
	}
}

// TestHygieneDeletesRowUnseenFor90Days locks in the hygiene gate itself: a
// row whose last_seen_at is more than 90 days old must be deleted outright
// by EffectiveApprovals, not merely excluded from the result.
func TestHygieneDeletesRowUnseenFor90Days(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	id := seedServer(t, db)
	approve(t, ap, id, "t", "FP", "sh", 91*day)

	if _, err := ap.EffectiveApprovals("u"); err != nil {
		t.Fatalf("EffectiveApprovals: %v", err)
	}

	var n int
	if err := db.QueryRow(`SELECT count(*) FROM mcp_tool_approvals WHERE server_id=? AND tool_name='t'`, id).Scan(&n); err != nil {
		t.Fatalf("count: %v", err)
	}
	if n != 0 {
		t.Fatal("a row unseen for more than 90 days must be deleted outright by the hygiene gate")
	}
}

// --- Task 18b: desc_hash ---

// TestPutWithDescHashStampsDescHash pins that PutWithDescHash writes the
// desc_hash column verbatim, the same way Put already writes schema_hash.
func TestPutWithDescHashStampsDescHash(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	id := seedServer(t, db)
	if err := ap.PutWithDescHash(id, "t", "FP", "sh", "dh1"); err != nil {
		t.Fatalf("PutWithDescHash: %v", err)
	}

	var got string
	if err := db.QueryRow(`SELECT desc_hash FROM mcp_tool_approvals WHERE server_id=? AND tool_name='t'`, id).Scan(&got); err != nil {
		t.Fatalf("select desc_hash: %v", err)
	}
	if got != "dh1" {
		t.Fatalf("expected stored desc_hash %q, got %q", "dh1", got)
	}
}

// TestPutWithDescHashRejectsWildcardToolName mirrors TestPutRejectsWildcardToolName:
// the '*' sentinel row must only ever be written by PutServerLevel.
func TestPutWithDescHashRejectsWildcardToolName(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	if err := ap.PutWithDescHash(seedServer(t, db), "*", "FP", "sh", "dh"); err == nil {
		t.Fatal("PutWithDescHash must reject tool_name '*' — use PutServerLevel for the server-level approval")
	}
}

// TestPutStoresEmptyDescHash locks in that the plain Put/PutServerLevel paths
// (used by the existing internal confirm-card writer, ApprovalsInternal)
// still store an empty desc_hash, exactly as before this task — they were
// not changed to accept one.
func TestPutStoresEmptyDescHash(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	id := seedServer(t, db)
	if err := ap.Put(id, "t", "FP", "sh"); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if err := ap.PutServerLevel(id, "FP"); err != nil {
		t.Fatalf("PutServerLevel: %v", err)
	}

	rows, err := ap.ListForServer(id)
	if err != nil {
		t.Fatalf("ListForServer: %v", err)
	}
	for _, r := range rows {
		if r.DescHash != "" {
			t.Fatalf("expected empty desc_hash from Put/PutServerLevel, got %q for tool %q", r.DescHash, r.ToolName)
		}
	}
}

// TestListForServerExposesLastSeenAtAndDescHash pins the two fields the new
// public /tools endpoint (route/v2/mcp_approvals.go) reads off ApprovalRow:
// LastSeenAt and DescHash must round-trip through ListForServer unchanged.
// This is a pure additional-data exposure — it must not touch any of
// ListForServer's existing StaleReason gate logic.
func TestListForServerExposesLastSeenAtAndDescHash(t *testing.T) {
	db := openTestDB(t)
	ap := &mcpApprovalService{db: db}
	id := seedServer(t, db)
	if err := ap.PutWithDescHash(id, "t", "FP", "sh", "dh1"); err != nil {
		t.Fatalf("PutWithDescHash: %v", err)
	}

	rows, err := ap.ListForServer(id)
	if err != nil {
		t.Fatalf("ListForServer: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("expected 1 row, got %d", len(rows))
	}
	if rows[0].DescHash != "dh1" {
		t.Fatalf("expected DescHash %q, got %q", "dh1", rows[0].DescHash)
	}
	if rows[0].LastSeenAt == 0 {
		t.Fatal("expected a nonzero LastSeenAt for a freshly-approved row")
	}
}
