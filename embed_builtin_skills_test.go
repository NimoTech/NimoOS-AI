package main

import (
	"encoding/json"
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

func TestBuiltinSeedVersionBumped(t *testing.T) {
	require.Equal(t, "7", service.BuiltinSeedVersion)
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
}
