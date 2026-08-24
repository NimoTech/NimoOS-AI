package service

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// BuiltinSeedVersion bumps whenever the embedded built-in catalog changes.
// The first byte of /var/lib/nimoos/skills/.version is compared against this.
const BuiltinSeedVersion = "16"

// SeedBuiltinSkills extracts an embedded FS of bundles into <root>/builtin/.
// Idempotent: if .version matches BuiltinSeedVersion, no work.
func SeedBuiltinSkills(root string, src fs.FS) error {
	versionFile := filepath.Join(root, ".version")
	if b, err := os.ReadFile(versionFile); err == nil &&
		strings.TrimSpace(string(b)) == BuiltinSeedVersion {
		return nil
	}
	target := filepath.Join(root, "builtin")
	tmp := target + ".tmp"
	_ = os.RemoveAll(tmp)
	if err := os.MkdirAll(tmp, 0o755); err != nil {
		return err
	}
	err := fs.WalkDir(src, "builtin-skills", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if p == "builtin-skills" {
			return nil
		}
		rel := strings.TrimPrefix(p, "builtin-skills/")
		dst := filepath.Join(tmp, rel)
		if d.IsDir() {
			return os.MkdirAll(dst, 0o755)
		}
		b, err := fs.ReadFile(src, p)
		if err != nil {
			return err
		}
		return os.WriteFile(dst, b, 0o644)
	})
	if err != nil {
		return err
	}
	_ = os.RemoveAll(target)
	if err := os.Rename(tmp, target); err != nil {
		return err
	}
	return os.WriteFile(versionFile, []byte(BuiltinSeedVersion), 0o644)
}

// SeedError signals a non-fatal seed error so callers can keep starting.
var SeedError = errors.New("builtin skills seed failed")
