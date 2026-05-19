package service

import (
	"os"
	"path/filepath"
	"testing"
)

func TestRebuildRuntimeView(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}

	// Seed two built-ins and one user skill.
	for _, id := range []string{"alpha", "beta"} {
		dir := s.BuiltinPath(id)
		_ = os.MkdirAll(dir, 0o755)
		_ = os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(`{
			"schema_version":1,"id":"`+id+`","name":"`+id+`","title":"`+id+`",
			"trigger":"auto","color":"blue","icon":"sparkle","description":"d",
			"version":"0.1.0","author":"Nimo","examples":[]}`), 0o644)
		_ = os.WriteFile(filepath.Join(dir, "SKILL.md"), []byte("## "+id), 0o644)
	}
	_, _ = s.CreateFromForm("42", CreateSkillReq{
		Name: "my-skill", Description: "d", Color: "blue", Trigger: "auto",
	})

	// Mark beta as uninstalled for user 42.
	uninstalled := map[string]bool{"beta": true}

	if err := RebuildRuntimeView(s, "42", uninstalled); err != nil {
		t.Fatal(err)
	}
	rt := s.RuntimePath("42")
	for _, want := range []string{"alpha", "my-skill"} {
		p := filepath.Join(rt, want)
		fi, err := os.Lstat(p)
		if err != nil {
			t.Fatalf("missing symlink %s: %v", want, err)
		}
		if fi.Mode()&os.ModeSymlink == 0 {
			t.Fatalf("%s is not a symlink", want)
		}
	}
	// beta should NOT be present
	if _, err := os.Lstat(filepath.Join(rt, "beta")); err == nil {
		t.Fatal("beta should have been excluded")
	}
}
