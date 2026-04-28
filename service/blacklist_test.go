package service

import (
	"database/sql"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

func newBlacklistTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	if err := migrate(db); err != nil {
		t.Fatal(err)
	}
	return db
}

func TestBlacklistCRUD(t *testing.T) {
	db := newBlacklistTestDB(t)
	defer db.Close()
	svc := &blacklistService{db: db}

	id, err := svc.Create("7", "**/*.key")
	if err != nil || id <= 0 {
		t.Fatalf("create failed: id=%d err=%v", id, err)
	}

	patterns, err := svc.List("7")
	if err != nil {
		t.Fatal(err)
	}
	if len(patterns) != 1 || patterns[0].Pattern != "**/*.key" {
		t.Fatalf("unexpected list: %+v", patterns)
	}

	// duplicate UNIQUE should error
	if _, err := svc.Create("7", "**/*.key"); err == nil {
		t.Fatal("expected unique violation")
	}

	if err := svc.Delete("7", id); err != nil {
		t.Fatal(err)
	}
	patterns, _ = svc.List("7")
	if len(patterns) != 0 {
		t.Fatalf("expected empty after delete, got %+v", patterns)
	}
}

func TestBlacklistListPatterns(t *testing.T) {
	db := newBlacklistTestDB(t)
	defer db.Close()
	svc := &blacklistService{db: db}
	if _, err := svc.Create("7", "a"); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Create("7", "b"); err != nil {
		t.Fatal(err)
	}
	got, err := svc.ListPatterns("7")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != "a" || got[1] != "b" {
		t.Fatalf("expected [a b], got %v", got)
	}
}

func TestBlacklistInvalidPattern(t *testing.T) {
	db := newBlacklistTestDB(t)
	defer db.Close()
	svc := &blacklistService{db: db}
	if _, err := svc.Create("7", "   "); err != ErrInvalidPattern {
		t.Fatalf("expected ErrInvalidPattern, got %v", err)
	}
	long := make([]byte, 257)
	for i := range long {
		long[i] = 'a'
	}
	if _, err := svc.Create("7", string(long)); err != ErrInvalidPattern {
		t.Fatalf("expected ErrInvalidPattern for too-long pattern, got %v", err)
	}
}
