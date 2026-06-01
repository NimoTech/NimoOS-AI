package main

import "embed"

//go:embed builtin-skills
var builtinSkillsFS embed.FS

// BuiltinSkillsFS exposes the embedded bundles to the service layer.
func BuiltinSkillsFS() embed.FS { return builtinSkillsFS }
