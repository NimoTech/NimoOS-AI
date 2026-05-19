package service

import (
	"errors"
	"path/filepath"
	"regexp"
)

var ErrBadSkillID = errors.New("invalid skill id")

// SkillsStore owns disk paths and writes for skill bundles.
type SkillsStore struct {
	Root string // typically /var/lib/nimoos/skills
}

func (s *SkillsStore) BuiltinPath(id string) string {
	return filepath.Join(s.Root, "builtin", id)
}

func (s *SkillsStore) UserPath(userID, id string) string {
	return filepath.Join(s.Root, "users", userID, id)
}

func (s *SkillsStore) RuntimePath(userID string) string {
	return filepath.Join(s.Root, ".runtime", userID)
}

// skillIDRe allows digit-leading IDs (e.g. "123-skill") so slugify of names
// like "123 skill" don't get rejected; first AND last char restricted to [a-z0-9]
// (no dash, no dot, no leading/trailing non-alnum) to keep this safe as a directory
// name and slash command.
var skillIDRe = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$`)

// ValidateSkillID enforces: lowercase, alnum-leading, dashes only, ≤64 chars.
// This becomes a slash command and a directory name; reject anything that
// could escape the bundle root or collide with a hidden file.
func ValidateSkillID(id string) error {
	if !skillIDRe.MatchString(id) {
		return ErrBadSkillID
	}
	return nil
}
