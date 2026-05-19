package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
)

func slugify(s string) string {
	s = strings.ToLower(strings.TrimSpace(s))
	var b strings.Builder
	dash := false
	for _, r := range s {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9'):
			b.WriteRune(r)
			dash = false
		default:
			if !dash && b.Len() > 0 {
				b.WriteRune('-')
				dash = true
			}
		}
	}
	out := strings.Trim(b.String(), "-")
	return out
}

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

const (
	MaxBundleBytes = 50 * 1024 * 1024
	MaxBundleFiles = 500
)

type SkillFileUpload struct {
	Path    string
	Content []byte
}

type CreateSkillReq struct {
	Name        string
	Title       string
	Description string
	Color       string
	Icon        string
	Trigger     string
	MD          string
	Examples    []string
	Scripts     []SkillFileUpload
}

var ErrDuplicateSkill = errors.New("skill already exists")
var ErrBadPath = errors.New("invalid file path in bundle")
var ErrBundleTooLarge = errors.New("bundle exceeds size limits")

// CreateFromForm writes a user bundle from the simple form upload. Atomic:
// builds in a temp dir, then rename()s into place.
func (s *SkillsStore) CreateFromForm(userID string, r CreateSkillReq) (*SkillManifest, error) {
	id := slugify(r.Name)
	if err := ValidateSkillID(id); err != nil {
		return nil, err
	}
	if r.Description == "" {
		return nil, fmt.Errorf("description required")
	}
	if len(r.MD) > MaxSkillMDBytes {
		return nil, fmt.Errorf("SKILL.md exceeds %d bytes (got %d)", MaxSkillMDBytes, len(r.MD))
	}

	dst := s.UserPath(userID, id)
	if _, err := os.Stat(dst); err == nil {
		return nil, ErrDuplicateSkill
	}
	// Also reject collision with built-ins
	if _, err := os.Stat(s.BuiltinPath(id)); err == nil {
		return nil, ErrDuplicateSkill
	}

	if err := checkUploads(r.Scripts); err != nil {
		return nil, err
	}

	icon := r.Icon
	if icon == "" {
		icon = "sparkle"
	}
	color := r.Color
	switch color {
	case "blue", "purple", "pink", "orange", "green", "teal", "slate":
	default:
		color = "blue"
	}
	triggerHuman := ""
	switch r.Trigger {
	case "auto":
		triggerHuman = "Automatic"
	case "slash":
		triggerHuman = "/" + id
	case "manual":
		triggerHuman = "Manual"
	default:
		r.Trigger = "auto"
		triggerHuman = "Automatic"
	}

	title := r.Title
	if title == "" {
		title = r.Name
	}
	md := r.MD
	if md == "" {
		md = fmt.Sprintf("## %s\n\n%s", title, r.Description)
	}

	m := &SkillManifest{
		SchemaVersion: 1,
		ID:            id,
		Name:          id,
		Title:         title,
		Description:   r.Description,
		Color:         color,
		Icon:          icon,
		Trigger:       r.Trigger,
		Examples:      r.Examples,
		Version:       "0.1.0",
		Author:        "You",
	}
	_ = triggerHuman // not stored; UI derives from trigger

	// Build in a sibling temp dir so we can rename() atomically.
	if err := os.MkdirAll(filepath.Join(s.Root, "users", userID), 0o755); err != nil {
		return nil, err
	}
	tmp, err := os.MkdirTemp(filepath.Join(s.Root, "users", userID), ".tmp-"+id+"-*")
	if err != nil {
		return nil, err
	}
	defer os.RemoveAll(tmp) // no-op if rename succeeded

	manifestBytes, _ := json.MarshalIndent(m, "", "  ")
	if err := os.WriteFile(filepath.Join(tmp, "manifest.json"), manifestBytes, 0o644); err != nil {
		return nil, err
	}
	if err := os.WriteFile(filepath.Join(tmp, "SKILL.md"), []byte(md), 0o644); err != nil {
		return nil, err
	}
	for _, f := range r.Scripts {
		full := filepath.Join(tmp, f.Path)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			return nil, err
		}
		if err := os.WriteFile(full, f.Content, 0o644); err != nil {
			return nil, err
		}
	}
	if err := os.Rename(tmp, dst); err != nil {
		return nil, err
	}
	return m, nil
}

var ErrNotFound = errors.New("skill not found")

func (s *SkillsStore) DeleteUser(userID, id string) error {
	if err := ValidateSkillID(id); err != nil {
		return err
	}
	dir := s.UserPath(userID, id)
	if _, err := os.Stat(dir); os.IsNotExist(err) {
		return ErrNotFound
	}
	return os.RemoveAll(dir)
}

// ReadFile reads a file from a bundle, safe against symlink escape.
//
// `filepath.Abs` is purely lexical; if the bundle contains a symlink to
// /etc/passwd, the lexical check would pass but the read would follow the
// symlink. We resolve the full real path with EvalSymlinks and re-check.
//
// In addition, we open with O_NOFOLLOW on the final component so a
// concurrent symlink-swap after the EvalSymlinks check can't TOCTOU us.
func (s *SkillsStore) ReadFile(bundleDir, relPath string) ([]byte, error) {
	cleaned := filepath.Clean(relPath)
	if filepath.IsAbs(cleaned) || strings.HasPrefix(cleaned, "..") {
		return nil, ErrBadPath
	}
	full := filepath.Join(bundleDir, cleaned)

	// Resolve symlinks in the FULL path. EvalSymlinks fails if the path
	// doesn't exist — propagate that as ErrNotExist.
	realFull, err := filepath.EvalSymlinks(full)
	if err != nil {
		return nil, err
	}
	realBundle, err := filepath.EvalSymlinks(bundleDir)
	if err != nil {
		return nil, err
	}
	if !strings.HasPrefix(realFull, realBundle+string(filepath.Separator)) &&
		realFull != realBundle {
		return nil, ErrBadPath
	}

	// O_NOFOLLOW guards against TOCTOU between EvalSymlinks and open(2).
	// Note: this prevents the *final* component from being a symlink; the
	// realBundle check above ensured the parent path is clean.
	f, err := os.OpenFile(realFull, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	// Cap reads at 10 MiB — bundle files larger than this are pathological.
	return io.ReadAll(io.LimitReader(f, 10*1024*1024))
}

func checkUploads(files []SkillFileUpload) error {
	if len(files) > MaxBundleFiles {
		return ErrBundleTooLarge
	}
	total := 0
	for _, f := range files {
		// Reject absolute, parent traversal, or symlink-style paths.
		if filepath.IsAbs(f.Path) ||
			strings.Contains(f.Path, "..") ||
			strings.HasPrefix(f.Path, "/") ||
			strings.Contains(f.Path, "\x00") {
			return ErrBadPath
		}
		cleaned := filepath.Clean(f.Path)
		if cleaned != f.Path || strings.HasPrefix(cleaned, "..") {
			return ErrBadPath
		}
		total += len(f.Content)
		if total > MaxBundleBytes {
			return ErrBundleTooLarge
		}
	}
	return nil
}
