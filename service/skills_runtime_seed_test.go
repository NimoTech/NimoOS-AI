package service

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
)

// Audit P2 (2026-09-03): a new built-in skill never reached users whose
// runtime view already existed — startup rebuilt views only for users with
// skill_state rows, and EnsureRuntimeView was a no-op on an existing dir.
// These tests pin the fix: every rebuilt view carries the seed version it was
// built from, a stale stamp triggers a rebuild on the next touch, and the
// startup sweep covers every user that has a view, rows or not.

func seedBuiltin(t *testing.T, s *SkillsStore, id string) {
	t.Helper()
	dir := s.BuiltinPath(id)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(filepath.Join(dir, "manifest.json"), []byte(`{
		"schema_version":1,"id":"`+id+`","name":"`+id+`","title":"`+id+`",
		"trigger":"auto","color":"blue","icon":"sparkle","description":"d",
		"version":"0.1.0","author":"Nimo","examples":[]}`), 0o644)
	_ = os.WriteFile(filepath.Join(dir, "SKILL.md"), []byte("## "+id), 0o644)
}

func hasLink(s *SkillsStore, uid, id string) bool {
	_, err := os.Lstat(filepath.Join(s.RuntimePath(uid), id))
	return err == nil
}

func TestRebuildRuntimeView_StampsSeedVersion(t *testing.T) {
	s := &SkillsStore{Root: t.TempDir(), SeedVersion: "v-test"}
	seedBuiltin(t, s, "alpha")
	if err := RebuildRuntimeView(s, "7", nil, nil); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(s.RuntimeSeedPath("7"))
	if err != nil {
		t.Fatalf("seed stamp missing: %v", err)
	}
	if string(b) != "v-test" {
		t.Fatalf("stamp=%q want v-test", b)
	}
	// The stamp lives beside the view, never inside it: the view is
	// ro-bind-mounted into the sandbox at /skill and must hold bundles only.
	if _, err := os.Lstat(filepath.Join(s.RuntimePath("7"), ".seed")); err == nil {
		t.Fatal("stamp must not be inside the runtime view")
	}
}

func TestEnsureRuntimeView_RebuildsOnlyWhenSeedVersionChanges(t *testing.T) {
	root := t.TempDir()
	store := &SkillsStore{Root: root, SeedVersion: "1"}
	seedBuiltin(t, store, "alpha")
	db, _ := NewDB(filepath.Join(root, "ai.db"))
	defer db.Close()
	svc := &skillsService{db: db, store: store}

	svc.EnsureRuntimeView("7") // fresh user, no skill_state rows
	if !hasLink(store, "7", "alpha") {
		t.Fatal("first EnsureRuntimeView must build the view")
	}

	// Same seed version: a new bundle appearing on disk without a version
	// bump is not a reason to rebuild (this is the pre-fix behaviour, kept).
	seedBuiltin(t, store, "beta")
	svc.EnsureRuntimeView("7")
	if hasLink(store, "7", "beta") {
		t.Fatal("no rebuild expected while the seed version is unchanged")
	}

	// Seed version bumped (new service binary with a new built-in): the
	// existing view is stale and must be rebuilt on the next touch.
	store.SeedVersion = "2"
	svc.EnsureRuntimeView("7")
	if !hasLink(store, "7", "beta") {
		t.Fatal("stale seed stamp must trigger a rebuild")
	}
	if b, _ := os.ReadFile(store.RuntimeSeedPath("7")); string(b) != "2" {
		t.Fatalf("stamp=%q want 2", b)
	}
}

func TestRebuildAllRuntimeViews_CoversUsersWithoutStateRows(t *testing.T) {
	root := t.TempDir()
	store := &SkillsStore{Root: root, SeedVersion: "1"}
	seedBuiltin(t, store, "alpha")
	db, _ := NewDB(filepath.Join(root, "ai.db"))
	defer db.Close()
	svc := &skillsService{db: db, store: store}

	// user 7: has a view, no rows (never touched the skills UI).
	svc.EnsureRuntimeView("7")
	// user 8: has rows (disabled alpha) and a view.
	if err := svc.SetEnabled("8", "alpha", false); err != nil {
		t.Fatal(err)
	}
	if hasLink(store, "8", "alpha") {
		t.Fatal("precondition: alpha disabled for user 8")
	}

	// New binary ships beta.
	seedBuiltin(t, store, "beta")
	store.SeedVersion = "2"
	if n := rebuildAllRuntimeViews(db, store, svc); n != 2 {
		t.Fatalf("rebuilt %d views, want 2", n)
	}
	if !hasLink(store, "7", "beta") {
		t.Fatal("user without skill_state rows must receive the new built-in")
	}
	if !hasLink(store, "8", "beta") || hasLink(store, "8", "alpha") {
		t.Fatal("user with rows keeps their overlay and receives the new built-in")
	}
}

func TestEnsureRuntimeView_RebuildsWhenSeedStampIsMissing(t *testing.T) {
	// The real first-upgrade state: views built by a binary that predates the
	// stamp have a view but no <uid>.seed. They must be treated as stale.
	root := t.TempDir()
	store := &SkillsStore{Root: root, SeedVersion: "1"}
	seedBuiltin(t, store, "alpha")
	db, _ := NewDB(filepath.Join(root, "ai.db"))
	defer db.Close()
	svc := &skillsService{db: db, store: store}

	svc.EnsureRuntimeView("7")
	if err := os.Remove(store.RuntimeSeedPath("7")); err != nil {
		t.Fatal(err)
	}
	seedBuiltin(t, store, "beta")
	svc.EnsureRuntimeView("7")
	if !hasLink(store, "7", "beta") {
		t.Fatal("a view without a seed stamp must be rebuilt")
	}
}

func TestEnsureRuntimeView_ConcurrentStaleTouchesKeepALiveView(t *testing.T) {
	// Right after a deploy every view is stale and the agent proxy and the
	// skills List land together. Without serialisation one rebuild's sweep
	// deleted the other's half-built dir and the <uid> link ended dangling.
	root := t.TempDir()
	store := &SkillsStore{Root: root, SeedVersion: "1"}
	seedBuiltin(t, store, "alpha")
	db, _ := NewDB(filepath.Join(root, "ai.db"))
	defer db.Close()
	svc := &skillsService{db: db, store: store}
	svc.EnsureRuntimeView("7")

	seedBuiltin(t, store, "beta")
	store.SeedVersion = "2"
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			svc.EnsureRuntimeView("7")
		}()
	}
	wg.Wait()

	target, err := os.Readlink(store.RuntimePath("7"))
	if err != nil {
		t.Fatalf("view symlink unreadable: %v", err)
	}
	if _, err := os.Stat(target); err != nil {
		t.Fatalf("view symlink dangling (%s): %v", target, err)
	}
	if !hasLink(store, "7", "alpha") || !hasLink(store, "7", "beta") {
		t.Fatal("rebuilt view must hold both built-ins")
	}
}
