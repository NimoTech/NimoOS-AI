package service

import (
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

// Skill is the on-disk shape of a user-defined skill, plus the runtime
// state for built-in skills (enabled flag, last_used, calls).
//
// Built-in skills are *not* stored in the user_skills table — they live
// in the BuiltinCatalog() slice and the user can only override their
// enabled state and "uninstall" them. Uninstalling adds a row to
// skill_state with uninstalled=1 and hides the skill from the list.
type Skill struct {
	ID           string    `json:"id"`
	Name         string    `json:"name"`
	Title        string    `json:"title"`
	Description  string    `json:"description"`
	Trigger      string    `json:"trigger"`       // auto | slash | manual
	TriggerHuman string    `json:"trigger_human"` // human-readable trigger summary
	Color        string    `json:"color"`         // blue|purple|pink|orange|green|teal|slate
	Icon         string    `json:"icon"`
	Enabled      bool      `json:"enabled"`
	System       bool      `json:"system"` // true → built-in
	Author       string    `json:"author"`
	LastUsed     string    `json:"last_used"`
	Calls        int       `json:"calls"`
	Files        []SkillFile `json:"files"`
	Examples     []string  `json:"examples"`
	MD           string    `json:"md"`
	CreatedAt    time.Time `json:"created_at,omitempty"`
}

type SkillFile struct {
	Name string `json:"name"`
	Size string `json:"size"`
}

var ErrSkillNotFound = errors.New("skill not found")
var ErrSkillSystem = errors.New("cannot modify built-in skill content")
var ErrSkillExists = errors.New("skill with that name already exists")
var ErrSkillInvalid = errors.New("invalid skill")

type skillsService struct {
	db *sql.DB
}

func validTrigger(t string) bool {
	return t == "auto" || t == "slash" || t == "manual"
}

func validColor(c string) bool {
	switch c {
	case "blue", "purple", "pink", "orange", "green", "teal", "slate":
		return true
	}
	return false
}

// ---------- Built-in catalog ----------

func builtinCatalog() []Skill {
	return []Skill{
		{
			ID: "photo-curator", Name: "photo-curator", Title: "Photo curator",
			Color: "purple", Icon: "image",
			Description:  "Automatically organize new photos by event, place, and people. Runs whenever the camera-import folder changes; clusters shots into scenes, picks the sharpest frame per moment, and writes albums to /Photos/Auto.",
			Trigger:      "auto",
			TriggerHuman: "Folder watcher · /Photos/_inbox",
			Enabled:      true, System: true, Author: "Nimo",
			LastUsed: "—", Calls: 0,
			Files: []SkillFile{
				{Name: "SKILL.md", Size: "4.2 KB"},
				{Name: "cluster.py", Size: "8.1 KB"},
				{Name: "rank.py", Size: "5.4 KB"},
				{Name: "prompts/", Size: "3 files"},
			},
			Examples: []string{
				"Group last week's iPhone imports by scene",
				"Pick the best shot of each subject",
				"Auto-build an album for the weekend trip",
			},
			MD: "## Photo curator\n\nCurate, cluster, and rank photos as they land on the NAS.\n\n### When it runs\n- Any change in **/Photos/_inbox** (debounced 90s)\n- On demand via `/photos curate <folder>`\n\n### What it does\n1. Embed each image with the on-device vision model\n2. Cluster by scene + location + time\n3. Score by sharpness, exposure, faces, and composition\n4. Write hero shots to `/Photos/Auto/<event>`\n\n### Guardrails\n- Originals are never deleted — duplicates moved to `/Photos/_archive`\n- Skips folders tagged `do-not-touch`\n- Asks before running on more than **2000 files**",
		},
		{
			ID: "duplicate-sweeper", Name: "duplicate-sweeper", Title: "Duplicate sweeper",
			Color: "orange", Icon: "copy",
			Description:  "Find exact and near-duplicate files across the NAS using perceptual hashes and CLIP embeddings. Always asks before deleting, and originals are kept in a 30-day recovery folder.",
			Trigger:      "manual",
			TriggerHuman: "Run from chat or schedule",
			Enabled:      true, System: true, Author: "Nimo",
			LastUsed: "—", Calls: 0,
			Files: []SkillFile{
				{Name: "SKILL.md", Size: "2.8 KB"},
				{Name: "phash.py", Size: "3.6 KB"},
				{Name: "embed.py", Size: "6.1 KB"},
			},
			Examples: []string{
				"Scan /Downloads for duplicates over 10 MB",
				"Find near-duplicates of /Photos/2024-trip",
				"How much can I reclaim from /Backup?",
			},
			MD: "## Duplicate sweeper\n\nDetect exact + perceptual duplicates and reclaim space safely.\n\n### Two-pass detection\n- **Pass 1** — SHA-256 for exact matches\n- **Pass 2** — Perceptual hash + CLIP for visual near-duplicates\n\n### Safe deletion\nOriginals are moved to `/Recycled` with a 30-day TTL — you can always undo.",
		},
		{
			ID: "doc-summarizer", Name: "doc-summarizer", Title: "Document summarizer",
			Color: "blue", Icon: "file",
			Description:  "Read PDFs, Markdown, and Word docs in a folder and produce a one-page recap, key entities, action items, and a Q&A index for later semantic search.",
			Trigger:      "slash",
			TriggerHuman: "/summarize <folder>",
			Enabled:      true, System: true, Author: "Nimo",
			LastUsed: "—", Calls: 0,
			Files: []SkillFile{
				{Name: "SKILL.md", Size: "3.1 KB"},
				{Name: "extract.py", Size: "4.4 KB"},
				{Name: "prompts/recap.md", Size: "1.2 KB"},
			},
			Examples: []string{
				"Summarize all Q1 invoices",
				"What's the total spend in /Receipts/2025?",
				"Make a one-pager from the lease folder",
			},
			MD: "## Document summarizer\n\nDistill a folder of documents into one page + structured fields.\n\n### Outputs\n- `recap.md` — 200-word overview\n- `entities.json` — people, orgs, amounts, dates\n- `actions.md` — action items with owners\n- `qa.index` — embeddings for semantic Q&A",
		},
		{
			ID: "media-transcoder", Name: "media-transcoder", Title: "Media transcoder",
			Color: "pink", Icon: "play",
			Description:  "Re-encode older video and audio formats to modern codecs (H.265, AV1, AAC) to save space. Preserves originals until you confirm.",
			Trigger:      "auto",
			TriggerHuman: "Nightly · 2 AM",
			Enabled:      false, System: true, Author: "Nimo",
			LastUsed: "—", Calls: 0,
			Files: []SkillFile{
				{Name: "SKILL.md", Size: "2.4 KB"},
				{Name: "ffmpeg-presets.json", Size: "1.1 KB"},
			},
			Examples: []string{
				"Re-encode all .MOV files in /Videos older than a year",
				"Estimate how much I'd save converting /Music to AAC",
			},
			MD: "## Media transcoder\n\nModernize older media without losing quality.\n\n### Defaults\n- Video → **H.265** (or AV1 if hardware supports)\n- Audio → **AAC 256 kbps**\n- Skips files already in the target codec",
		},
	}
}

// ---------- DB ----------

// List returns built-in + user skills for the user, with overlay state applied.
func (s *skillsService) List(userID string) ([]Skill, error) {
	// Load overlay state for built-ins.
	stateRows, err := s.db.Query(
		`SELECT skill_id, enabled, uninstalled, last_used, calls
		 FROM skill_state WHERE user_id=?`, userID)
	if err != nil {
		return nil, err
	}
	defer stateRows.Close()
	type state struct {
		enabled     bool
		uninstalled bool
		lastUsed    string
		calls       int
	}
	stateMap := map[string]state{}
	for stateRows.Next() {
		var id, lastUsed string
		var enabled, uninstalled int
		var calls int
		if err := stateRows.Scan(&id, &enabled, &uninstalled, &lastUsed, &calls); err != nil {
			return nil, err
		}
		stateMap[id] = state{
			enabled: enabled != 0, uninstalled: uninstalled != 0,
			lastUsed: lastUsed, calls: calls,
		}
	}

	out := []Skill{}
	for _, b := range builtinCatalog() {
		st, ok := stateMap[b.ID]
		if ok {
			if st.uninstalled {
				continue
			}
			b.Enabled = st.enabled
			if st.lastUsed != "" {
				b.LastUsed = st.lastUsed
			}
			if st.calls > 0 {
				b.Calls = st.calls
			}
		}
		out = append(out, b)
	}

	// Load user skills.
	rows, err := s.db.Query(
		`SELECT id, name, title, description, trigger_kind, trigger_human,
		        color, icon, enabled, author, last_used, calls,
		        files_json, examples_json, md, created_at
		 FROM user_skills WHERE user_id=? ORDER BY created_at ASC`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var sk Skill
		var enabled int
		var filesJSON, examplesJSON sql.NullString
		var lastUsed sql.NullString
		var created time.Time
		if err := rows.Scan(
			&sk.ID, &sk.Name, &sk.Title, &sk.Description,
			&sk.Trigger, &sk.TriggerHuman, &sk.Color, &sk.Icon,
			&enabled, &sk.Author, &lastUsed, &sk.Calls,
			&filesJSON, &examplesJSON, &sk.MD, &created,
		); err != nil {
			return nil, err
		}
		sk.Enabled = enabled != 0
		sk.System = false
		sk.CreatedAt = created
		if lastUsed.Valid && lastUsed.String != "" {
			sk.LastUsed = lastUsed.String
		} else {
			sk.LastUsed = "—"
		}
		if filesJSON.Valid && filesJSON.String != "" {
			_ = json.Unmarshal([]byte(filesJSON.String), &sk.Files)
		}
		if examplesJSON.Valid && examplesJSON.String != "" {
			_ = json.Unmarshal([]byte(examplesJSON.String), &sk.Examples)
		}
		if sk.Files == nil {
			sk.Files = []SkillFile{}
		}
		if sk.Examples == nil {
			sk.Examples = []string{}
		}
		out = append(out, sk)
	}
	return out, rows.Err()
}

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

func (s *skillsService) Create(userID string, in Skill) (*Skill, error) {
	in.Name = strings.TrimSpace(in.Name)
	in.Description = strings.TrimSpace(in.Description)
	if in.Name == "" || in.Description == "" {
		return nil, ErrSkillInvalid
	}
	slug := slugify(in.Name)
	if slug == "" {
		return nil, ErrSkillInvalid
	}
	if !validTrigger(in.Trigger) {
		in.Trigger = "auto"
	}
	if !validColor(in.Color) {
		in.Color = "blue"
	}
	if in.Icon == "" {
		in.Icon = "sparkle"
	}
	if in.Title == "" {
		in.Title = in.Name
	}
	if in.TriggerHuman == "" {
		switch in.Trigger {
		case "auto":
			in.TriggerHuman = "Automatic"
		case "slash":
			in.TriggerHuman = "/" + slug
		default:
			in.TriggerHuman = "Manual"
		}
	}
	if in.Files == nil {
		in.Files = []SkillFile{{Name: "SKILL.md", Size: "—"}}
	}
	if in.Examples == nil {
		in.Examples = []string{}
	}
	if in.MD == "" {
		in.MD = "## " + in.Title + "\n\n" + in.Description
	}

	// Reject collisions with built-in IDs and existing user skills.
	for _, b := range builtinCatalog() {
		if b.ID == slug {
			return nil, ErrSkillExists
		}
	}
	var exists int
	_ = s.db.QueryRow(
		`SELECT COUNT(1) FROM user_skills WHERE user_id=? AND id=?`,
		userID, slug,
	).Scan(&exists)
	if exists > 0 {
		return nil, ErrSkillExists
	}

	filesJSON, _ := json.Marshal(in.Files)
	examplesJSON, _ := json.Marshal(in.Examples)
	_, err := s.db.Exec(
		`INSERT INTO user_skills(
			id, user_id, name, title, description, trigger_kind, trigger_human,
			color, icon, enabled, author, last_used, calls,
			files_json, examples_json, md, created_at
		) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)`,
		slug, userID, slug, in.Title, in.Description, in.Trigger, in.TriggerHuman,
		in.Color, in.Icon, boolToInt(true), "You", "Just now", 0,
		string(filesJSON), string(examplesJSON), in.MD,
	)
	if err != nil {
		return nil, err
	}
	return s.Get(userID, slug)
}

// Update merges patch fields. Only `enabled` is allowed on built-ins.
func (s *skillsService) Update(userID, id string, patch Skill, fields map[string]bool) (*Skill, error) {
	// Built-in?
	for _, b := range builtinCatalog() {
		if b.ID == id {
			if !fields["enabled"] && len(fields) > 0 {
				return nil, ErrSkillSystem
			}
			enabled := b.Enabled
			if fields["enabled"] {
				enabled = patch.Enabled
			}
			return nil, s.upsertBuiltinState(userID, id, enabled, false)
		}
	}
	// User skill
	row := s.db.QueryRow(
		`SELECT id FROM user_skills WHERE user_id=? AND id=?`, userID, id)
	var dummy string
	if err := row.Scan(&dummy); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrSkillNotFound
		}
		return nil, err
	}

	sets := []string{}
	args := []any{}
	if fields["enabled"] {
		sets = append(sets, "enabled=?")
		args = append(args, boolToInt(patch.Enabled))
	}
	if fields["description"] {
		sets = append(sets, "description=?")
		args = append(args, strings.TrimSpace(patch.Description))
	}
	if fields["title"] {
		sets = append(sets, "title=?")
		args = append(args, strings.TrimSpace(patch.Title))
	}
	if fields["md"] {
		sets = append(sets, "md=?")
		args = append(args, patch.MD)
	}
	if fields["color"] && validColor(patch.Color) {
		sets = append(sets, "color=?")
		args = append(args, patch.Color)
	}
	if fields["trigger"] && validTrigger(patch.Trigger) {
		sets = append(sets, "trigger_kind=?")
		args = append(args, patch.Trigger)
	}
	if len(sets) == 0 {
		return s.Get(userID, id)
	}
	args = append(args, userID, id)
	_, err := s.db.Exec(
		`UPDATE user_skills SET `+strings.Join(sets, ", ")+
			` WHERE user_id=? AND id=?`, args...)
	if err != nil {
		return nil, err
	}
	return s.Get(userID, id)
}

// Delete: built-in → mark uninstalled. User skill → DELETE row.
func (s *skillsService) Delete(userID, id string) error {
	for _, b := range builtinCatalog() {
		if b.ID == id {
			return s.upsertBuiltinState(userID, id, false, true)
		}
	}
	res, err := s.db.Exec(
		`DELETE FROM user_skills WHERE user_id=? AND id=?`, userID, id)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrSkillNotFound
	}
	return nil
}

// RecordRun bumps the calls counter and last_used after a sandbox run.
func (s *skillsService) RecordRun(userID, id string) error {
	// Built-in?
	for _, b := range builtinCatalog() {
		if b.ID == id {
			// upsert with current state, incrementing calls.
			_, err := s.db.Exec(
				`INSERT INTO skill_state(user_id, skill_id, enabled, uninstalled, last_used, calls)
				 VALUES(?,?,?,0,?,1)
				 ON CONFLICT(user_id, skill_id) DO UPDATE SET
				 last_used=excluded.last_used, calls=calls+1`,
				userID, id, boolToInt(b.Enabled), "Just now",
			)
			return err
		}
	}
	_, err := s.db.Exec(
		`UPDATE user_skills SET last_used='Just now', calls=calls+1
		 WHERE user_id=? AND id=?`, userID, id)
	return err
}

func (s *skillsService) upsertBuiltinState(userID, id string, enabled, uninstalled bool) error {
	_, err := s.db.Exec(
		`INSERT INTO skill_state(user_id, skill_id, enabled, uninstalled, last_used, calls)
		 VALUES(?,?,?,?,'',0)
		 ON CONFLICT(user_id, skill_id) DO UPDATE SET
		 enabled=excluded.enabled, uninstalled=excluded.uninstalled`,
		userID, id, boolToInt(enabled), boolToInt(uninstalled),
	)
	return err
}

