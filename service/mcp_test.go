package service

import (
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
)

func newMcpSvc(t *testing.T) *mcpService {
	db, err := NewDB(filepath.Join(t.TempDir(), "ai.db"))
	if err != nil {
		t.Fatalf("NewDB: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return &mcpService{db: db}
}

func TestMcp_CRUDAndIsolation(t *testing.T) {
	s := newMcpSvc(t)
	m := &McpServer{
		UserID: "u1", Name: "github", Transport: "http",
		URL: "https://mcp.example/x", Args: "[]", Env: "{}",
		Headers: "ENC_HEADERS", Enabled: true,
	}
	if err := s.CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}
	if m.ID == 0 {
		t.Fatal("ID not set")
	}

	got, err := s.GetMcpServer(m.ID, "u1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got.Name != "github" || got.Headers != "ENC_HEADERS" || !got.Enabled {
		t.Fatalf("roundtrip mismatch: %+v", got)
	}

	if _, err := s.GetMcpServer(m.ID, "other"); !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("expected ErrNoRows for other user, got %v", err)
	}

	m2 := &McpServer{UserID: "u1", Name: "off", Transport: "http", Args: "[]", Env: "{}", Enabled: false}
	if err := s.CreateMcpServer(m2); err != nil {
		t.Fatalf("create2: %v", err)
	}
	enabled, err := s.ListEnabledMcpServers("u1")
	if err != nil {
		t.Fatalf("list enabled: %v", err)
	}
	if len(enabled) != 1 || enabled[0].Name != "github" {
		t.Fatalf("enabled filter wrong: %+v", enabled)
	}

	got.Name = "github2"
	got.Enabled = false
	if err := s.UpdateMcpServer(got); err != nil {
		t.Fatalf("update: %v", err)
	}
	again, _ := s.GetMcpServer(m.ID, "u1")
	if again.Name != "github2" || again.Enabled {
		t.Fatalf("update not applied: %+v", again)
	}

	if err := s.DeleteMcpServer(m.ID, "other"); !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("delete wrong user should be ErrNoRows, got %v", err)
	}
	if err := s.DeleteMcpServer(m.ID, "u1"); err != nil {
		t.Fatalf("delete: %v", err)
	}
}
