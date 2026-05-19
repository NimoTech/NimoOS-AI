package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
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

// SkillManifest is the on-disk shape of <bundle>/manifest.json.
type SkillManifest struct {
	SchemaVersion int      `json:"schema_version"`
	ID            string   `json:"id"`
	Name          string   `json:"name"`
	Title         string   `json:"title"`
	Description   string   `json:"description"`
	Color         string   `json:"color"`
	Icon          string   `json:"icon"`
	Trigger       string   `json:"trigger"`
	Examples      []string `json:"examples"`
	Entrypoint    string   `json:"entrypoint,omitempty"`
	Permissions   struct {
		Network       bool     `json:"network"`
		WritablePaths []string `json:"writable_paths"`
	} `json:"permissions"`
	Version string `json:"version"`
	Author  string `json:"author"`
}

// MaxSkillMDBytes caps SKILL.md so a malicious 10 MiB markdown can't blow
// the LLM context window when injected as a system prompt. 50 KiB is well
// above any reasonable hand-written instruction file.
const MaxSkillMDBytes = 50 * 1024

// LoadManifest reads <dir>/manifest.json and validates required fields.
// Also enforces SKILL.md size cap if present.
func (s *SkillsStore) LoadManifest(dir string) (*SkillManifest, error) {
	b, err := os.ReadFile(filepath.Join(dir, "manifest.json"))
	if err != nil {
		return nil, fmt.Errorf("read manifest: %w", err)
	}
	var m SkillManifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("parse manifest: %w", err)
	}
	if err := ValidateSkillID(m.ID); err != nil {
		return nil, fmt.Errorf("manifest.id: %w", err)
	}
	if m.Trigger != "auto" && m.Trigger != "slash" && m.Trigger != "manual" {
		return nil, fmt.Errorf("manifest.trigger must be auto|slash|manual, got %q", m.Trigger)
	}
	switch m.Color {
	case "blue", "purple", "pink", "orange", "green", "teal", "slate":
	default:
		m.Color = "blue"
	}
	if m.Title == "" {
		m.Title = m.Name
	}
	if m.Examples == nil {
		m.Examples = []string{}
	}
	// SKILL.md must exist and stay under MaxSkillMDBytes.
	if info, err := os.Stat(filepath.Join(dir, "SKILL.md")); err != nil {
		return nil, fmt.Errorf("SKILL.md missing: %w", err)
	} else if info.Size() > MaxSkillMDBytes {
		return nil, fmt.Errorf("SKILL.md exceeds %d bytes (got %d)", MaxSkillMDBytes, info.Size())
	}
	return &m, nil
}

func (s *SkillsStore) ListBuiltin() ([]*SkillManifest, error) {
	return s.listDir(filepath.Join(s.Root, "builtin"))
}

func (s *SkillsStore) ListUser(userID string) ([]*SkillManifest, error) {
	return s.listDir(filepath.Join(s.Root, "users", userID))
}

func (s *SkillsStore) listDir(dir string) ([]*SkillManifest, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	out := make([]*SkillManifest, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		m, err := s.LoadManifest(filepath.Join(dir, e.Name()))
		if err != nil {
			// Bad bundle: skip but don't fail the whole list.
			continue
		}
		out = append(out, m)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}
