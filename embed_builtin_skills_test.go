package main

import (
	"encoding/json"
	"testing"

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
	require.Equal(t, "6", service.BuiltinSeedVersion)
}
