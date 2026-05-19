package service

import (
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
