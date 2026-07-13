package service

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
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

func TestSkillsStore_DeleteUser(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}
	_, err := s.CreateFromForm("42", CreateSkillReq{
		Name: "x", Description: "d", Color: "blue", Trigger: "auto",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.DeleteUser("42", "x"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(s.UserPath("42", "x")); !os.IsNotExist(err) {
		t.Fatalf("expected gone, err=%v", err)
	}
}

func TestSkillsStore_DeleteUser_NotFound(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}
	if err := s.DeleteUser("42", "ghost"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expected ErrNotFound, got %v", err)
	}
}

func TestSkillsStore_ReadFile_RejectsSymlinkEscape(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}

	// Build a "bundle" with a symlink trying to point at the host system.
	bundleDir := filepath.Join(root, "evil")
	_ = os.MkdirAll(bundleDir, 0o755)
	target := filepath.Join(root, "outside.txt")
	_ = os.WriteFile(target, []byte("secret"), 0o644)
	_ = os.Symlink(target, filepath.Join(bundleDir, "escape"))

	if _, err := s.ReadFile(bundleDir, "escape"); err == nil {
		t.Fatal("expected symlink escape to be rejected")
	}
}

func TestSkillsStore_ReadFile_AllowsNormalFile(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}
	bundleDir := filepath.Join(root, "ok")
	_ = os.MkdirAll(bundleDir, 0o755)
	_ = os.WriteFile(filepath.Join(bundleDir, "SKILL.md"), []byte("## hi"), 0o644)
	data, err := s.ReadFile(bundleDir, "SKILL.md")
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	if string(data) != "## hi" {
		t.Fatalf("got %q", string(data))
	}
}

func TestSkillsService_ListMergesBuiltinAndUser(t *testing.T) {
	root := t.TempDir()
	store := &SkillsStore{Root: root}
	dir := store.BuiltinPath("photo-curator")
	_ = os.MkdirAll(dir, 0o755)
	_ = os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(`{
		"schema_version":1,"id":"photo-curator","name":"photo-curator","title":"Photo curator",
		"trigger":"auto","color":"purple","icon":"image","description":"d",
		"version":"0.1.0","author":"Nimo","examples":[]}`), 0o644)
	_ = os.WriteFile(filepath.Join(dir, "SKILL.md"), []byte("## hi"), 0o644)

	dbPath := filepath.Join(t.TempDir(), "ai.db")
	db, _ := NewDB(dbPath)
	defer db.Close()
	svc := &skillsService{db: db, store: store}

	_, _ = svc.CreateUser("42", CreateSkillReq{
		Name: "my-skill", Description: "d", Color: "blue", Trigger: "auto",
	})

	list, err := svc.List("42")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 {
		t.Fatalf("got %d skills, want 2: %+v", len(list), list)
	}
	// Built-in first (system=true), user after.
	if !list[0].System || list[0].ID != "photo-curator" {
		t.Errorf("[0]=%+v", list[0])
	}
	if list[1].System || list[1].ID != "my-skill" {
		t.Errorf("[1]=%+v", list[1])
	}
}

func TestSkillsService_DeleteRebuildsRuntimeView(t *testing.T) {
	root := t.TempDir()
	store := &SkillsStore{Root: root}
	db, _ := NewDB(filepath.Join(root, "ai.db"))
	defer db.Close()
	svc := &skillsService{db: db, store: store}

	// seed one user skill + rebuild
	_, _ = svc.CreateUser("42", CreateSkillReq{
		Name: "my-skill", Description: "d", Color: "blue", Trigger: "auto",
	})
	// CreateUser should have rebuilt; verify the symlink exists
	if _, err := os.Lstat(filepath.Join(store.RuntimePath("42"), "my-skill")); err != nil {
		t.Fatal("symlink missing after create")
	}

	// delete → runtime view should not see it
	_ = svc.Delete("42", "my-skill")
	if _, err := os.Lstat(filepath.Join(store.RuntimePath("42"), "my-skill")); !os.IsNotExist(err) {
		t.Fatalf("symlink still present, err=%v", err)
	}
}

func TestValidateSkillDescription(t *testing.T) {
	cases := []struct {
		name string
		desc string
		ok   bool
	}{
		{"valid", "Find duplicates via SHA-256 hashing.", true},
		{"256 runes ok", strings.Repeat("a", 256), true},
		{"empty", "", false},
		{"newline", "line1\nline2", false},
		{"carriage return", "line1\rline2", false},
		{"angle brackets", "use <tag> here", false},
		{"tab", "a\tb", false},
		{"del control", "a\x7fb", false},
		{"257 runes", strings.Repeat("很", 257), false},
	}
	for _, c := range cases {
		err := validateSkillDescription(c.desc)
		if c.ok && err != nil {
			t.Errorf("%s: unexpected error: %v", c.name, err)
		}
		if !c.ok && !errors.Is(err, ErrBadDescription) {
			t.Errorf("%s: want ErrBadDescription, got %v", c.name, err)
		}
	}
}

func TestSkillsStore_CreateFromForm_RejectsBadDescription(t *testing.T) {
	s := &SkillsStore{Root: t.TempDir()}
	_, err := s.CreateFromForm("42", CreateSkillReq{
		Name:        "bad-desc",
		Description: "first line\nsecond line",
		Trigger:     "auto",
	})
	if !errors.Is(err, ErrBadDescription) {
		t.Fatalf("want ErrBadDescription, got %v", err)
	}
	if _, statErr := os.Stat(s.UserPath("42", "bad-desc")); !os.IsNotExist(statErr) {
		t.Fatalf("bundle dir must not be created on rejection")
	}
}
