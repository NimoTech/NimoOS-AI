package service

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

func TestSkillsMigration_AddsTables(t *testing.T) {
	db, err := NewDB(filepath.Join(t.TempDir(), "ai.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	for _, table := range []string{"user_skills", "skill_state"} {
		var n int
		if err := db.QueryRow(
			`SELECT count(1) FROM sqlite_master WHERE type='table' AND name=?`,
			table).Scan(&n); err != nil {
			t.Fatal(err)
		}
		if n != 1 {
			t.Fatalf("table %s missing", table)
		}
	}
}

func TestSkillsMigration_ReducesUserSkillsColumns(t *testing.T) {
	db, _ := NewDB(filepath.Join(t.TempDir(), "ai.db"))
	defer db.Close()
	rows, _ := db.Query(`PRAGMA table_info(user_skills)`)
	defer rows.Close()
	cols := map[string]bool{}
	for rows.Next() {
		var (
			cid   int
			name  string
			ctype string
			nn    int
			dflt  sql.NullString
			pk    int
		)
		if err := rows.Scan(&cid, &name, &ctype, &nn, &dflt, &pk); err != nil {
			t.Fatal(err)
		}
		cols[name] = true
	}
	// New slim schema: only state + indexing fields.
	for _, want := range []string{"id", "user_id", "last_used", "calls", "created_at"} {
		if !cols[want] {
			t.Errorf("missing column %s", want)
		}
	}
	// Bulky content fields are gone.
	for _, gone := range []string{"description", "md", "files_json", "examples_json"} {
		if cols[gone] {
			t.Errorf("column %s should have been dropped", gone)
		}
	}
}
