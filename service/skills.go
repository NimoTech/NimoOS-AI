package service

import (
	"database/sql"
	"errors"
	"os"
	"path/filepath"
)

type Skill struct {
	ID           string      `json:"id"`
	Name         string      `json:"name"`
	Title        string      `json:"title"`
	Description  string      `json:"description"`
	Trigger      string      `json:"trigger"`
	TriggerHuman string      `json:"trigger_human"`
	Color        string      `json:"color"`
	Icon         string      `json:"icon"`
	Enabled      bool        `json:"enabled"`
	System       bool        `json:"system"`
	Author       string      `json:"author"`
	LastUsed     string      `json:"last_used"`
	Calls        int         `json:"calls"`
	Files        []SkillFile `json:"files"`
	Examples     []string    `json:"examples"`
	MD           string      `json:"md"`
}

type SkillFile struct {
	Name string `json:"name"`
	Size string `json:"size"`
}

// Sentinel errors. Reuse ErrNotFound/ErrDuplicateSkill from skills_store.go.
var ErrSkillNotFound = ErrNotFound
var ErrSkillExists = ErrDuplicateSkill
var ErrSkillSystem = errors.New("cannot modify built-in skill content")
var ErrSkillInvalid = errors.New("invalid skill")

type skillsService struct {
	db    *sql.DB
	store *SkillsStore
}

// Store gives external access to the underlying disk store (for handler-side
// path resolution like GET /skills/:id/files/*).
func (s *skillsService) Store() *SkillsStore { return s.store }

// ReadFileInBundle exposes the symlink-safe file reader to handlers.
func (s *skillsService) ReadFileInBundle(dir, rel string) ([]byte, error) {
	return s.store.ReadFile(dir, rel)
}

type skillState struct {
	enabled, uninstalled bool
	lastUsed             string
	calls                int
}

// loadRuntimeFilterMaps returns the uninstalled and disabled skill_id sets
// used to filter the per-user runtime view. Disabled skills (`enabled=0`)
// stay installed but are hidden from the agent so the LLM cannot see or
// load them.
func (s *skillsService) loadRuntimeFilterMaps(userID string) (uninstalled, disabled map[string]bool, err error) {
	uninstalled = map[string]bool{}
	disabled = map[string]bool{}
	rows, err := s.db.Query(
		`SELECT skill_id, enabled, uninstalled FROM skill_state WHERE user_id=?`,
		userID)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		var en, un int
		if err := rows.Scan(&id, &en, &un); err != nil {
			continue
		}
		if un != 0 {
			uninstalled[id] = true
		}
		if en == 0 && un == 0 {
			disabled[id] = true
		}
	}
	return uninstalled, disabled, nil
}

// rebuildAfter atomically rebuilds the user's runtime view after a
// mutating call (CreateUser / SetEnabled / Delete). Best-effort: a
// rebuild failure logs but doesn't fail the call (the next service
// start re-runs RebuildRuntimeView for every user).
func (s *skillsService) rebuildAfter(userID string) {
	u, d, _ := s.loadRuntimeFilterMaps(userID)
	_ = RebuildRuntimeView(s.store, userID, u, d)
}

// EnsureRuntimeView builds .runtime/<uid>/ if it's missing. A fresh user
// (no skill_state rows) is skipped by the startup rebuild loop, so the
// agent's skill index would render empty until the user toggled
// something in the UI. Callers on hot paths (agent proxy, List) invoke
// this lazily so the agent can always discover built-in skills.
func (s *skillsService) EnsureRuntimeView(userID string) {
	if _, err := os.Stat(s.store.RuntimePath(userID)); errors.Is(err, os.ErrNotExist) {
		s.rebuildAfter(userID)
	}
}

// loadStateMap returns the skill_state overlay for a user.
func (s *skillsService) loadStateMap(userID string) (map[string]skillState, error) {
	out := map[string]skillState{}
	rows, err := s.db.Query(
		`SELECT skill_id, enabled, uninstalled, last_used, calls
		 FROM skill_state WHERE user_id=?`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id, lu string
		var en, un, calls int
		if err := rows.Scan(&id, &en, &un, &lu, &calls); err != nil {
			return nil, err
		}
		out[id] = skillState{enabled: en != 0, uninstalled: un != 0, lastUsed: lu, calls: calls}
	}
	return out, rows.Err()
}

func valOrDash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

func humanSize(n int64) string {
	const k = 1024
	switch {
	case n < k:
		return itoaSkill(int(n)) + " B"
	case n < k*k:
		return itoaSkill(int(n/k)) + ".0 KB"
	default:
		return itoaSkill(int(n/(k*k))) + ".0 MB"
	}
}

func itoaSkill(n int) string {
	if n == 0 {
		return "0"
	}
	digits := []byte{}
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	return string(digits)
}

// listBundleFiles returns shallow file listings for the UI.
func (s *skillsService) listBundleFiles(dir string) []SkillFile {
	out := []SkillFile{}
	entries, _ := os.ReadDir(dir)
	for _, e := range entries {
		full := filepath.Join(dir, e.Name())
		info, err := os.Stat(full)
		if err != nil {
			continue
		}
		size := humanSize(info.Size())
		if e.IsDir() {
			subs, _ := os.ReadDir(full)
			size = "(" + itoaSkill(len(subs)) + " files)"
		}
		out = append(out, SkillFile{Name: e.Name(), Size: size})
	}
	return out
}

// manifestToSkill projects a SkillManifest + state overlay to the API DTO.
func (s *skillsService) manifestToSkill(m *SkillManifest, system bool, st skillState, author string) Skill {
	if author == "" {
		if system {
			author = m.Author
		} else {
			author = "You"
		}
	}
	triggerHuman := m.Trigger
	switch m.Trigger {
	case "auto":
		triggerHuman = "Automatic"
	case "slash":
		triggerHuman = "/" + m.Name
	case "manual":
		triggerHuman = "Manual"
	}
	return Skill{
		ID:           m.ID,
		Name:         m.Name,
		Title:        m.Title,
		Description:  m.Description,
		Trigger:      m.Trigger,
		TriggerHuman: triggerHuman,
		Color:        m.Color,
		Icon:         m.Icon,
		Enabled:      st.enabled,
		System:       system,
		Author:       author,
		LastUsed:     valOrDash(st.lastUsed),
		Calls:        st.calls,
		Examples:     m.Examples,
	}
}

func (s *skillsService) loadUserSkillState(userID, id string) (skillState, bool) {
	row := s.db.QueryRow(
		`SELECT last_used, calls FROM user_skills WHERE user_id=? AND id=?`,
		userID, id)
	var st skillState
	st.enabled = true
	err := row.Scan(&st.lastUsed, &st.calls)
	if err == sql.ErrNoRows {
		return st, false
	}
	return st, err == nil
}

// List returns built-in + user skills with state overlay applied.
// Built-ins come first (sorted by id), users come after (sorted by id).
func (s *skillsService) List(userID string) ([]Skill, error) {
	state, err := s.loadStateMap(userID)
	if err != nil {
		return nil, err
	}
	s.EnsureRuntimeView(userID)
	out := []Skill{}
	builtIns, err := s.store.ListBuiltin()
	if err != nil {
		return nil, err
	}
	for _, m := range builtIns {
		st, hadRow := state[m.ID]
		if st.uninstalled {
			continue
		}
		if !hadRow {
			st.enabled = true
		}
		sk := s.manifestToSkill(m, true, st, m.Author)
		sk.Files = s.listBundleFiles(s.store.BuiltinPath(m.ID))
		md, _ := s.store.ReadFile(s.store.BuiltinPath(m.ID), "SKILL.md")
		sk.MD = string(md)
		out = append(out, sk)
	}
	userSkills, err := s.store.ListUser(userID)
	if err != nil {
		return nil, err
	}
	for _, m := range userSkills {
		st, ok := s.loadUserSkillState(userID, m.ID)
		if !ok {
			st.enabled = true
		}
		// Per-user enabled flag also lives in skill_state for unified toggle handling.
		if stateRow, has := state[m.ID]; has {
			st.enabled = stateRow.enabled
		}
		sk := s.manifestToSkill(m, false, st, "You")
		sk.Files = s.listBundleFiles(s.store.UserPath(userID, m.ID))
		md, _ := s.store.ReadFile(s.store.UserPath(userID, m.ID), "SKILL.md")
		sk.MD = string(md)
		out = append(out, sk)
	}
	return out, nil
}

// CreateUser writes a new bundle and inserts a user_skills row.
func (s *skillsService) CreateUser(userID string, r CreateSkillReq) (*Skill, error) {
	m, err := s.store.CreateFromForm(userID, r)
	if err != nil {
		return nil, err
	}
	defer s.rebuildAfter(userID)
	_, _ = s.db.Exec(
		`INSERT INTO user_skills(id, user_id, last_used, calls) VALUES(?,?,?,?)`,
		m.ID, userID, "Just now", 0,
	)
	st := skillState{enabled: true, lastUsed: "Just now"}
	sk := s.manifestToSkill(m, false, st, "You")
	sk.Files = s.listBundleFiles(s.store.UserPath(userID, m.ID))
	md, _ := s.store.ReadFile(s.store.UserPath(userID, m.ID), "SKILL.md")
	sk.MD = string(md)
	return &sk, nil
}

// InstallOrReplace creates a user skill bundle, overwriting any existing
// bundle with the same id instead of failing with ErrDuplicateSkill.
//
// Used by the internal component-install endpoint (Task 7): Go components
// (e.g. the Lark integration) reinstall their bundled skill(s) on every
// startup, and re-registering the same skill twice should be idempotent
// rather than 409. CreateFromForm itself has no overwrite mode, so this
// deletes the existing bundle first when one is present.
func (s *skillsService) InstallOrReplace(userID string, r CreateSkillReq) (*Skill, error) {
	id := slugify(r.Name)
	if err := ValidateSkillID(id); err != nil {
		return nil, err
	}
	if _, err := os.Stat(s.store.UserPath(userID, id)); err == nil {
		if err := s.Delete(userID, id); err != nil {
			return nil, err
		}
	}
	return s.CreateUser(userID, r)
}

// SetEnabled updates the enabled state. Works for both built-in and user skills.
func (s *skillsService) SetEnabled(userID, id string, enabled bool) error {
	defer s.rebuildAfter(userID)
	// Built-in?
	if _, err := os.Stat(s.store.BuiltinPath(id)); err == nil {
		_, err := s.db.Exec(
			`INSERT INTO skill_state(user_id, skill_id, enabled, uninstalled)
			 VALUES(?,?,?,0)
			 ON CONFLICT(user_id, skill_id) DO UPDATE SET enabled=excluded.enabled`,
			userID, id, boolToIntSkill(enabled),
		)
		return err
	}
	// User skill?
	if _, err := os.Stat(s.store.UserPath(userID, id)); err != nil {
		return ErrNotFound
	}
	_, err := s.db.Exec(
		`INSERT INTO skill_state(user_id, skill_id, enabled, uninstalled)
		 VALUES(?,?,?,0)
		 ON CONFLICT(user_id, skill_id) DO UPDATE SET enabled=excluded.enabled`,
		userID, id, boolToIntSkill(enabled),
	)
	return err
}

// Delete removes a user bundle OR marks a built-in as uninstalled.
//
// If the on-disk directory is already gone (orphaned state row left over
// from a previous half-failure), we still clean up the DB rows. This
// prevents "ghost" rows that can never be deleted. (Fix 2.2.)
func (s *skillsService) Delete(userID, id string) error {
	defer s.rebuildAfter(userID)
	if _, err := os.Stat(s.store.BuiltinPath(id)); err == nil {
		_, err := s.db.Exec(
			`INSERT INTO skill_state(user_id, skill_id, enabled, uninstalled)
			 VALUES(?,?,0,1)
			 ON CONFLICT(user_id, skill_id) DO UPDATE SET uninstalled=1`,
			userID, id,
		)
		return err
	}
	// User skill: try to delete the directory; tolerate "already gone".
	if err := s.store.DeleteUser(userID, id); err != nil && !errors.Is(err, ErrNotFound) {
		return err
	}
	// Always clean DB rows.
	_, _ = s.db.Exec(`DELETE FROM user_skills WHERE user_id=? AND id=?`, userID, id)
	_, _ = s.db.Exec(`DELETE FROM skill_state WHERE user_id=? AND skill_id=?`, userID, id)
	return nil
}

// RecordRun bumps last_used+calls for either kind of skill.
func (s *skillsService) RecordRun(userID, id string) error {
	if _, err := os.Stat(s.store.BuiltinPath(id)); err == nil {
		_, err := s.db.Exec(
			`INSERT INTO skill_state(user_id, skill_id, enabled, uninstalled, last_used, calls)
			 VALUES(?,?,1,0,?,1)
			 ON CONFLICT(user_id, skill_id) DO UPDATE SET last_used=excluded.last_used, calls=calls+1`,
			userID, id, "Just now",
		)
		return err
	}
	_, err := s.db.Exec(
		`UPDATE user_skills SET last_used='Just now', calls=calls+1
		 WHERE user_id=? AND id=?`, userID, id)
	return err
}

func boolToIntSkill(b bool) int {
	if b {
		return 1
	}
	return 0
}

// ---------- v0 compatibility shims (used by handler until Task 12) ----------

// Get looks up a single skill by scanning List output.
func (s *skillsService) Get(userID, id string) (*Skill, error) {
	list, err := s.List(userID)
	if err != nil {
		return nil, err
	}
	for i := range list {
		if list[i].ID == id {
			return &list[i], nil
		}
	}
	return nil, ErrSkillNotFound
}

// Create is a thin shim over CreateUser for the existing handler.
// Accepts the legacy Skill-shaped input.
func (s *skillsService) Create(userID string, in Skill) (*Skill, error) {
	return s.CreateUser(userID, CreateSkillReq{
		Name:        in.Name,
		Title:       in.Title,
		Description: in.Description,
		Trigger:     in.Trigger,
		Color:       in.Color,
		Icon:        in.Icon,
		MD:          in.MD,
		Examples:    in.Examples,
	})
}

// Update is a thin shim over SetEnabled / disk-based writes for the handler.
// Only `enabled` is supported on built-ins; user skill metadata is not yet
// editable from the handler (full rewrite in Task 12).
func (s *skillsService) Update(userID, id string, patch Skill, fields map[string]bool) (*Skill, error) {
	if fields["enabled"] {
		if err := s.SetEnabled(userID, id, patch.Enabled); err != nil {
			return nil, err
		}
	}
	return s.Get(userID, id)
}
