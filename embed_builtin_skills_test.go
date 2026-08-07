package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/stretchr/testify/require"

	"github.com/NimoTech/NimoOS-AI/service"
)

func TestFileReaderSkillEmbedded(t *testing.T) {
	b, err := builtinSkillsFS.ReadFile("builtin-skills/file-reader/manifest.json")
	require.NoError(t, err)
	var m service.SkillManifest
	require.NoError(t, json.Unmarshal(b, &m))
	require.Equal(t, "file-reader", m.ID)
	require.Equal(t, "auto", m.Trigger)

	_, err = builtinSkillsFS.ReadFile("builtin-skills/file-reader/SKILL.md")
	require.NoError(t, err)
}

// Deliberately not pinned to a literal version. The previous form asserted
// == "8" and went stale the moment the catalog moved on: v9, v10 and v11 all
// landed without anyone updating it, and it only surfaced once CI existed to
// run it. What actually has to hold is that the constant is a usable version
// and that seeding records exactly it — if the recorded value ever diverged,
// SeedBuiltinSkills would either re-extract on every start or, worse, never
// re-extract after a catalog update.
func TestBuiltinSeedVersionRecordedOnDisk(t *testing.T) {
	require.Regexp(t, `^[0-9]+$`, service.BuiltinSeedVersion)

	root := t.TempDir()
	require.NoError(t, service.SeedBuiltinSkills(root, builtinSkillsFS))

	b, err := os.ReadFile(filepath.Join(root, ".version"))
	require.NoError(t, err)
	require.Equal(t, service.BuiltinSeedVersion, strings.TrimSpace(string(b)))
}

func TestDesktopAppBuilderSkillEmbedded(t *testing.T) {
	b, err := builtinSkillsFS.ReadFile("builtin-skills/desktop-app-builder/manifest.json")
	require.NoError(t, err)
	var m service.SkillManifest
	require.NoError(t, json.Unmarshal(b, &m))
	require.Equal(t, "desktop-app-builder", m.ID)
	require.Equal(t, "auto", m.Trigger)

	// Description is injected into the system prompt: single line, no
	// angle brackets, ≤256 runes (mirrors validateSkillDescription).
	require.LessOrEqual(t, utf8.RuneCountInString(m.Description), 256)
	require.NotContains(t, m.Description, "\n")
	require.NotContains(t, m.Description, "<")
	require.NotContains(t, m.Description, ">")

	_, err = builtinSkillsFS.ReadFile("builtin-skills/desktop-app-builder/SKILL.md")
	require.NoError(t, err)

	_, err = builtinSkillsFS.ReadFile("builtin-skills/desktop-app-builder/references/app-contract.md")
	require.NoError(t, err)

	_, err = builtinSkillsFS.ReadFile("builtin-skills/desktop-app-builder/references/widget-contract.md")
	require.NoError(t, err)
}

func TestAllBuiltinBundlesPassValidation(t *testing.T) {
	root := t.TempDir()
	require.NoError(t, service.SeedBuiltinSkills(root, builtinSkillsFS))
	store := &service.SkillsStore{Root: root}
	ms, err := store.ListBuiltin()
	require.NoError(t, err)
	ids := make([]string, 0, len(ms))
	for _, m := range ms {
		ids = append(ids, m.ID)
	}
	require.Contains(t, ids, "desktop-app-builder")
	// 7 pre-existing bundles + desktop-app-builder. A silently-skipped
	// (invalid) bundle would make this count drop.
	require.Len(t, ms, 8)
}
