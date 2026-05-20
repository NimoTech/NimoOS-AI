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

	if err := RebuildRuntimeView(s, "42", uninstalled, nil); err != nil {
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

// Disabled (paused) built-in and user skills must be omitted from the
// runtime view so the agent can't see or load them.
func TestRebuildRuntimeView_OmitsDisabled(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}

	for _, id := range []string{"alpha", "beta"} {
		dir := s.BuiltinPath(id)
		_ = os.MkdirAll(dir, 0o755)
		_ = os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(`{
			"schema_version":1,"id":"`+id+`","name":"`+id+`","title":"`+id+`",
			"trigger":"auto","color":"blue","icon":"sparkle","description":"d",
			"version":"0.1.0","author":"Nimo","examples":[]}`), 0o644)
		_ = os.WriteFile(filepath.Join(dir, "SKILL.md"), []byte("## "+id), 0o644)
	}
	user, _ := s.CreateFromForm("42", CreateSkillReq{
		Name: "hello-world", Description: "d", Color: "blue", Trigger: "auto",
	})

	disabled := map[string]bool{"beta": true, user.ID: true}
	if err := RebuildRuntimeView(s, "42", nil, disabled); err != nil {
		t.Fatal(err)
	}
	rt := s.RuntimePath("42")
	if _, err := os.Lstat(filepath.Join(rt, "alpha")); err != nil {
		t.Fatalf("alpha (enabled) should still be present: %v", err)
	}
	for _, gone := range []string{"beta", user.ID} {
		if _, err := os.Lstat(filepath.Join(rt, gone)); err == nil {
			t.Fatalf("disabled skill %s should have been excluded", gone)
		}
	}
}

func TestRebuildRuntimeView_CleansStaleArtifacts(t *testing.T) {
	root := t.TempDir()
	s := &SkillsStore{Root: root}

	// Pre-seed leftover .v* dirs and .tmp-* symlinks as a crashed rebuild
	// would have. Also one belonging to a different user — must be left alone.
	rtDir := filepath.Dir(s.RuntimePath("7"))
	if err := os.MkdirAll(rtDir, 0o755); err != nil {
		t.Fatal(err)
	}
	stale := []string{"7.v1000", "7.v2000", "9.v3000"}
	for _, d := range stale {
		if err := os.MkdirAll(filepath.Join(rtDir, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	tmpLink := filepath.Join(rtDir, "7.tmp-9999")
	if err := os.Symlink(filepath.Join(rtDir, "7.v1000"), tmpLink); err != nil {
		t.Fatal(err)
	}

	// Need one built-in so the rebuild has something to do.
	dir := s.BuiltinPath("alpha")
	_ = os.MkdirAll(dir, 0o755)
	_ = os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(`{
		"schema_version":1,"id":"alpha","name":"alpha","title":"alpha",
		"trigger":"auto","color":"blue","icon":"sparkle","description":"d",
		"version":"0.1.0","author":"Nimo","examples":[]}`), 0o644)
	_ = os.WriteFile(filepath.Join(dir, "SKILL.md"), []byte("## alpha"), 0o644)

	if err := RebuildRuntimeView(s, "7", nil, nil); err != nil {
		t.Fatal(err)
	}

	for _, d := range []string{"7.v1000", "7.v2000"} {
		if _, err := os.Stat(filepath.Join(rtDir, d)); !os.IsNotExist(err) {
			t.Fatalf("stale dir %s should have been removed", d)
		}
	}
	if _, err := os.Lstat(tmpLink); !os.IsNotExist(err) {
		t.Fatal("stale tmp symlink should have been removed")
	}
	if _, err := os.Stat(filepath.Join(rtDir, "9.v3000")); err != nil {
		t.Fatalf("other user's dir should be untouched: %v", err)
	}

	// The fresh symlink points to a .v dir that still exists.
	target, err := os.Readlink(s.RuntimePath("7"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(target); err != nil {
		t.Fatalf("live target should exist: %v", err)
	}
}
