package service

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
)

// openTestDB uses the real NewDB to ensure both the DSN (_foreign_keys=on) and migrations are covered.
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")
	db, err := NewDB(path)
	if err != nil {
		t.Fatalf("NewDB: %v", err)
	}
	t.Cleanup(func() { db.Close(); os.Remove(path) })
	return db
}

func TestNewTablesExist(t *testing.T) {
	db := openTestDB(t)
	for _, tbl := range []string{"mcp_server_runtime", "mcp_server_schemas", "mcp_tool_approvals"} {
		var n int
		err := db.QueryRow(
			`SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?`, tbl).Scan(&n)
		if err != nil || n != 1 {
			t.Fatalf("table %s missing (n=%d err=%v)", tbl, n, err)
		}
	}
}

func TestMcpServersHasNoteColumn(t *testing.T) {
	db := openTestDB(t)
	if _, err := db.Exec(
		`INSERT INTO mcp_servers (user_id,name,transport,url,command,args,env,headers,enabled,created_at,updated_at,note)
		 VALUES ('u','n','http','http://x','','[]','{}','',1,0,0,'hello')`); err != nil {
		t.Fatalf("note column missing: %v", err)
	}
}

// Safety precondition 1: foreign keys must be ON; otherwise ON DELETE CASCADE silently fails, leaving orphaned approval rows.
func TestForeignKeysEnabled(t *testing.T) {
	db := openTestDB(t)
	var on int
	if err := db.QueryRow(`PRAGMA foreign_keys`).Scan(&on); err != nil {
		t.Fatalf("pragma: %v", err)
	}
	if on != 1 {
		t.Fatal("foreign_keys must be ON; CASCADE silently no-ops otherwise")
	}
}

// Safety precondition 2: server IDs must never be reused. Without AUTOINCREMENT, SQLite uses max(rowid)+1,
// so deleting the server with the highest ID and creating a new one would reuse that same ID, inheriting all the deleted server's approvals.
func TestServerIDNeverReused(t *testing.T) {
	db := openTestDB(t)
	ins := func(name string) int64 {
		res, err := db.Exec(
			`INSERT INTO mcp_servers (user_id,name,transport,url,command,args,env,headers,enabled,created_at,updated_at)
			 VALUES ('u',?,'http','http://x','','[]','{}','',1,0,0)`, name)
		if err != nil {
			t.Fatalf("insert: %v", err)
		}
		id, _ := res.LastInsertId()
		return id
	}
	oldID := ins("gmail-work")
	if _, err := db.Exec(
		`INSERT INTO mcp_tool_approvals (server_id,tool_name,identity_fp,schema_hash,approved_at,last_seen_at)
		 VALUES (?,'send_email','fp','sh',1,1)`, oldID); err != nil {
		t.Fatalf("approval insert: %v", err)
	}
	if _, err := db.Exec(`DELETE FROM mcp_servers WHERE id=?`, oldID); err != nil {
		t.Fatalf("delete: %v", err)
	}
	newID := ins("notion")
	if newID <= oldID {
		t.Fatalf("id reused: old=%d new=%d — mcp_servers.id MUST be AUTOINCREMENT", oldID, newID)
	}
	var n int
	db.QueryRow(`SELECT count(*) FROM mcp_tool_approvals WHERE server_id=?`, newID).Scan(&n)
	if n != 0 {
		t.Fatalf("new server inherited %d approvals from the deleted one", n)
	}
}
