package service

import (
	"os"
	"path/filepath"
	"testing"
	"testing/fstest"
)

func TestSeedBuiltinSkills(t *testing.T) {
	src := fstest.MapFS{
		"builtin-skills/alpha/manifest.json": &fstest.MapFile{Data: []byte(`{
			"schema_version":1,"id":"alpha","name":"alpha","title":"Alpha",
			"trigger":"auto","color":"blue","icon":"sparkle","description":"d",
			"version":"0.1.0","author":"Nimo","examples":[]}`)},
		"builtin-skills/alpha/SKILL.md": &fstest.MapFile{Data: []byte("## Alpha")},
	}
	root := t.TempDir()
	if err := SeedBuiltinSkills(root, src); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "builtin", "alpha", "manifest.json")); err != nil {
		t.Fatal(err)
	}
	// Second run is a no-op
	if err := SeedBuiltinSkills(root, src); err != nil {
		t.Fatal(err)
	}
}

func TestNewService_SeedsBuiltins(t *testing.T) {
	dir := t.TempDir()
	src := simpleSeedFS()
	skillsRoot := filepath.Join(dir, "skills")
	if err := SeedBuiltinSkills(skillsRoot, src); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(skillsRoot, "builtin", "alpha", "SKILL.md")); err != nil {
		t.Fatal(err)
	}
}

func simpleSeedFS() fstest.MapFS {
	return fstest.MapFS{
		"builtin-skills/alpha/manifest.json": &fstest.MapFile{Data: []byte(`{
			"schema_version":1,"id":"alpha","name":"alpha","title":"Alpha",
			"trigger":"auto","color":"blue","icon":"sparkle","description":"d",
			"version":"0.1.0","author":"Nimo","examples":[]}`)},
		"builtin-skills/alpha/SKILL.md": &fstest.MapFile{Data: []byte("## Alpha")},
	}
}
