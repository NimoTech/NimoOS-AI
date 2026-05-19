package service

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestSkillsStore_BundlePath(t *testing.T) {
	s := &SkillsStore{Root: "/var/lib/nimoos/skills"}

	if got := s.BuiltinPath("photo-curator"); got != "/var/lib/nimoos/skills/builtin/photo-curator" {
		t.Fatalf("BuiltinPath: %s", got)
	}
	if got := s.UserPath("42", "my-skill"); got != "/var/lib/nimoos/skills/users/42/my-skill" {
		t.Fatalf("UserPath: %s", got)
	}
	if got := s.RuntimePath("42"); got != "/var/lib/nimoos/skills/.runtime/42" {
		t.Fatalf("RuntimePath: %s", got)
	}
}

func TestSkillsStore_RejectsBadID(t *testing.T) {
	s := &SkillsStore{Root: t.TempDir()}
	cases := []string{"", "..", "../etc", "with/slash", "with space", "with.dot",
		"abc-", "-abc", "123-"}
	for _, id := range cases {
		if err := ValidateSkillID(id); err == nil {
			t.Errorf("ValidateSkillID(%q) should fail", id)
		}
	}
	good := []string{"photo-curator", "abc", "x123", "doc-summarizer-2", "123-skill"}
	for _, id := range good {
		if err := ValidateSkillID(id); err != nil {
			t.Errorf("ValidateSkillID(%q) unexpected: %v", id, err)
		}
	}
	_ = filepath.Join // keep import
	_ = s             // keep s used
}

func TestSkillsStore_LoadManifest(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}

	dir := s.BuiltinPath("photo-curator")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	manifest := `{
		"schema_version": 1,
		"id": "photo-curator",
		"name": "photo-curator",
		"title": "Photo curator",
		"description": "Cluster photos",
		"color": "purple",
		"icon": "image",
		"trigger": "auto",
		"examples": ["Cluster last week"],
		"version": "0.1.0",
		"author": "Nimo"
	}`
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"),
		[]byte(manifest), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "SKILL.md"),
		[]byte("## hi"), 0o644); err != nil {
		t.Fatal(err)
	}

	m, err := s.LoadManifest(dir)
	if err != nil {
		t.Fatalf("LoadManifest: %v", err)
	}
	if m.ID != "photo-curator" || m.Color != "purple" || m.Trigger != "auto" {
		t.Fatalf("bad manifest: %+v", m)
	}
	if len(m.Examples) != 1 || m.Examples[0] != "Cluster last week" {
		t.Fatalf("examples: %+v", m.Examples)
	}
}

func TestSkillsStore_LoadManifest_RejectsBad(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}
	dir := filepath.Join(root, "broken")
	_ = os.MkdirAll(dir, 0o755)
	_ = os.WriteFile(filepath.Join(dir, "manifest.json"),
		[]byte(`{"id":"","trigger":"bogus"}`), 0o644)
	if _, err := s.LoadManifest(dir); err == nil {
		t.Fatal("expected error")
	}
}

func TestSkillsStore_ListBuiltin(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}

	for _, id := range []string{"alpha", "beta"} {
		dir := s.BuiltinPath(id)
		_ = os.MkdirAll(dir, 0o755)
		_ = os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(fmt.Sprintf(
			`{"schema_version":1,"id":%q,"name":%q,"title":"%s","trigger":"auto","color":"blue","icon":"sparkle","description":"d","version":"0.1.0","author":"Nimo","examples":[]}`,
			id, id, id)), 0o644)
		_ = os.WriteFile(filepath.Join(dir, "SKILL.md"), []byte("## "+id), 0o644)
	}

	got, err := s.ListBuiltin()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("len=%d", len(got))
	}
	// Listed sorted by id
	if got[0].ID != "alpha" || got[1].ID != "beta" {
		t.Fatalf("order: %+v", got)
	}
}

func TestSkillsStore_ListUser(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}
	dir := s.UserPath("42", "my-skill")
	_ = os.MkdirAll(dir, 0o755)
	_ = os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(
		`{"schema_version":1,"id":"my-skill","name":"my-skill","title":"My","trigger":"manual","color":"green","icon":"sparkle","description":"d","version":"0.1.0","author":"You","examples":[]}`),
		0o644)
	_ = os.WriteFile(filepath.Join(dir, "SKILL.md"), []byte("## My"), 0o644)

	got, err := s.ListUser("42")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].ID != "my-skill" {
		t.Fatalf("got: %+v", got)
	}
}

func TestSkillsStore_ListUser_EmptyForUnknownUser(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}
	got, err := s.ListUser("nobody")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Fatalf("expected 0, got %d", len(got))
	}
}

func TestSkillsStore_CreateFromForm(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}

	m, err := s.CreateFromForm("42", CreateSkillReq{
		Name:        "invoice-tagger",
		Title:       "Invoice tagger",
		Description: "Tag invoices",
		Color:       "orange",
		Icon:        "file",
		Trigger:     "slash",
		MD:          "## Invoice tagger\n\nReads PDFs.",
		Examples:    []string{"Tag last quarter"},
		Scripts: []SkillFileUpload{
			{Path: "scripts/extract.py", Content: []byte("print('hi')")},
		},
	})
	if err != nil {
		t.Fatalf("CreateFromForm: %v", err)
	}
	if m.ID != "invoice-tagger" {
		t.Fatalf("id: %s", m.ID)
	}

	dir := s.UserPath("42", "invoice-tagger")
	if _, err := os.Stat(filepath.Join(dir, "manifest.json")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "SKILL.md")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "scripts", "extract.py")); err != nil {
		t.Fatal(err)
	}
}

func TestSkillsStore_CreateFromForm_RejectsDuplicate(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}
	req := CreateSkillReq{Name: "dup", Description: "x", Color: "blue", Trigger: "auto"}
	if _, err := s.CreateFromForm("42", req); err != nil {
		t.Fatal(err)
	}
	if _, err := s.CreateFromForm("42", req); err == nil {
		t.Fatal("expected duplicate error")
	}
}

func TestSkillsStore_CreateFromForm_RejectsPathEscape(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}
	_, err := s.CreateFromForm("42", CreateSkillReq{
		Name: "bad", Description: "x", Color: "blue", Trigger: "auto",
		Scripts: []SkillFileUpload{
			{Path: "../escape", Content: []byte("x")},
		},
	})
	if err == nil {
		t.Fatal("expected escape error")
	}
}
