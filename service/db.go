package service

import (
	"database/sql"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

// Protocol type for cloud providers
type Protocol string

const (
	ProtocolOpenAI    Protocol = "openai"
	ProtocolAnthropic Protocol = "anthropic"
)

// Provider stores per-user cloud provider config
type Provider struct {
	ID           int64
	UserID       string   // string form of JWT integer user ID
	Name         string
	BaseURL      string
	APIKey       string   // AES-GCM encrypted, base64-encoded
	Protocol     Protocol
	Enabled      bool
	DefaultModel string
	ProviderType string   // NEW: deepseek|openai|anthropic|qwen|ollama|other
	CreatedAt    time.Time
}

// PrivacyPolicy stores per-user privacy settings
type PrivacyPolicy struct {
	ID               int64  `json:"id"`
	UserID           string `json:"user_id"`
	AllowRemote      bool   `json:"allow_remote"`   // false = lock to local only
	DefaultBackend   string `json:"default_backend"` // "local" | "cloud"
	EscalationPrompt bool   `json:"escalation_prompt"` // show confirmation before escalating to cloud
}

// ChatSession represents a conversation
type ChatSession struct {
	ID        int64     `json:"id"`
	UserID    string    `json:"user_id"`
	Title     string    `json:"title"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// ChatMessage stores a single message in OpenAI format
type ChatMessage struct {
	ID        int64     `json:"id"`
	SessionID int64     `json:"session_id"`
	Role      string    `json:"role"` // "system"|"user"|"assistant"
	Content   string    `json:"content"`
	CreatedAt time.Time `json:"created_at"`
}

// NewDB opens (or creates) the SQLite database and runs migrations
func NewDB(path string) (*sql.DB, error) {
	db, err := sql.Open("sqlite3", path+"?_journal_mode=WAL&_foreign_keys=on")
	if err != nil {
		return nil, err
	}
	if err := migrate(db); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

type Model struct {
	ID               int64  `json:"id"`
	Name             string `json:"name"`
	Source           string `json:"source"`
	SizeBytes        int64  `json:"size_bytes"`
	Quantization     string `json:"quantization"`
	DownloadedAt     string `json:"downloaded_at"`
	LastUsedAt       string `json:"last_used_at"`
	SupportsThinking bool   `json:"supports_thinking"`
}

const (
	ModelSourceOllama      = "ollama"
	ModelSourceHuggingFace = "huggingface"
	ModelSourceOpenVINO    = "openvino"
)

func migrate(db *sql.DB) error {
	_, err := db.Exec(`
	CREATE TABLE IF NOT EXISTS providers (
		id            INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id       TEXT NOT NULL,
		name          TEXT NOT NULL,
		base_url      TEXT NOT NULL,
		api_key       TEXT NOT NULL DEFAULT '',
		protocol      TEXT NOT NULL DEFAULT 'openai',
		enabled       INTEGER NOT NULL DEFAULT 1,
		default_model TEXT NOT NULL DEFAULT '',
		created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
	);
	CREATE INDEX IF NOT EXISTS idx_providers_user_id ON providers(user_id);

	CREATE TABLE IF NOT EXISTS mcp_servers (
		id          INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id     TEXT NOT NULL,
		name        TEXT NOT NULL,
		transport   TEXT NOT NULL,
		url         TEXT NOT NULL DEFAULT '',
		command     TEXT NOT NULL DEFAULT '',
		args        TEXT NOT NULL DEFAULT '[]',
		env         TEXT NOT NULL DEFAULT '{}',
		headers     TEXT NOT NULL DEFAULT '',
		enabled     INTEGER NOT NULL DEFAULT 1,
		created_at  INTEGER NOT NULL,
		updated_at  INTEGER NOT NULL
	);
	CREATE INDEX IF NOT EXISTS idx_mcp_servers_user_id ON mcp_servers(user_id);

	CREATE TABLE IF NOT EXISTS privacy_policies (
		id                INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id           TEXT NOT NULL UNIQUE,
		allow_remote      INTEGER NOT NULL DEFAULT 1,
		default_backend   TEXT NOT NULL DEFAULT 'local',
		escalation_prompt INTEGER NOT NULL DEFAULT 1
	);
	CREATE INDEX IF NOT EXISTS idx_policies_user_id ON privacy_policies(user_id);

	CREATE TABLE IF NOT EXISTS chat_sessions (
		id         INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id    TEXT NOT NULL,
		title      TEXT NOT NULL DEFAULT '',
		created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
		updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
	);
	CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON chat_sessions(user_id);

	CREATE TABLE IF NOT EXISTS chat_messages (
		id         INTEGER PRIMARY KEY AUTOINCREMENT,
		session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
		role       TEXT NOT NULL,
		content    TEXT NOT NULL,
		created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
	);
	CREATE INDEX IF NOT EXISTS idx_messages_session_id ON chat_messages(session_id);

	CREATE TABLE IF NOT EXISTS models (
		id           INTEGER PRIMARY KEY AUTOINCREMENT,
		name         TEXT NOT NULL UNIQUE,
		source       TEXT NOT NULL DEFAULT 'ollama',
		size_bytes   INTEGER NOT NULL DEFAULT 0,
		quantization TEXT NOT NULL DEFAULT '',
		downloaded_at TEXT NOT NULL DEFAULT '',
		last_used_at  TEXT NOT NULL DEFAULT ''
	);

	CREATE TABLE IF NOT EXISTS provider_models (
		id          INTEGER PRIMARY KEY AUTOINCREMENT,
		provider_id INTEGER NOT NULL,
		model_name  TEXT NOT NULL,
		source      TEXT NOT NULL DEFAULT 'fetched',
		favorite    INTEGER NOT NULL DEFAULT 0,
		created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
		UNIQUE(provider_id, model_name),
		FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
	);
	CREATE INDEX IF NOT EXISTS idx_provider_models_pid ON provider_models(provider_id);

	CREATE TABLE IF NOT EXISTS hard_blacklist (
		id         INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id    TEXT NOT NULL,
		pattern    TEXT NOT NULL,
		created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
		UNIQUE(user_id, pattern)
	);
	CREATE INDEX IF NOT EXISTS idx_blacklist_user ON hard_blacklist(user_id);

	CREATE TABLE IF NOT EXISTS user_skills (
		id         TEXT NOT NULL,
		user_id    TEXT NOT NULL,
		last_used  TEXT NOT NULL DEFAULT '',
		calls      INTEGER NOT NULL DEFAULT 0,
		created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
		PRIMARY KEY (user_id, id)
	);

	CREATE TABLE IF NOT EXISTS skill_state (
		user_id     TEXT NOT NULL,
		skill_id    TEXT NOT NULL,
		enabled     INTEGER NOT NULL DEFAULT 1,
		uninstalled INTEGER NOT NULL DEFAULT 0,
		last_used   TEXT NOT NULL DEFAULT '',
		calls       INTEGER NOT NULL DEFAULT 0,
		PRIMARY KEY (user_id, skill_id)
	);
	`)
	if err != nil {
		return err
	}
	// One-time migration from the fat user_skills schema (pre-0.4.x-alpha).
	// If we see legacy bulk columns, the content is already on disk (or never
	// existed): just drop and recreate the table.
	rows, err := db.Query(`PRAGMA table_info(user_skills)`)
	if err == nil {
		legacy := false
		for rows.Next() {
			var cid int
			var name, ctype string
			var nn, pk int
			var dflt sql.NullString
			rows.Scan(&cid, &name, &ctype, &nn, &dflt, &pk)
			if name == "description" || name == "md" || name == "files_json" {
				legacy = true
			}
		}
		rows.Close()
		if legacy {
			_, _ = db.Exec(`DROP TABLE user_skills`)
			_, _ = db.Exec(`CREATE TABLE user_skills (
				id         TEXT NOT NULL,
				user_id    TEXT NOT NULL,
				last_used  TEXT NOT NULL DEFAULT '',
				calls      INTEGER NOT NULL DEFAULT 0,
				created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
				PRIMARY KEY (user_id, id)
			)`)
		}
	}

	// Idempotent column additions for existing databases
	_, _ = db.Exec(`ALTER TABLE providers ADD COLUMN default_model TEXT NOT NULL DEFAULT ''`)
	_, _ = db.Exec(`ALTER TABLE providers ADD COLUMN provider_type TEXT NOT NULL DEFAULT ''`)

	// One-time backfill: classify rows whose provider_type is still empty.
	rows, err = db.Query(`SELECT id, base_url, protocol FROM providers WHERE provider_type=''`)
	if err == nil {
		type row struct {
			id       int64
			baseURL  string
			protocol string
		}
		var todo []row
		for rows.Next() {
			var r row
			if err := rows.Scan(&r.id, &r.baseURL, &r.protocol); err == nil {
				todo = append(todo, r)
			}
		}
		rows.Close()
		for _, r := range todo {
			pt := ClassifyProvider(r.baseURL, Protocol(r.protocol))
			_, _ = db.Exec(`UPDATE providers SET provider_type=? WHERE id=?`, pt, r.id)
		}
	}
	// One-time backfill: surface each provider's existing default_model as a
	// favorite manual model so upgraded installs see it immediately. Idempotent
	// via the UNIQUE(provider_id, model_name) constraint.
	_, _ = db.Exec(
		`INSERT OR IGNORE INTO provider_models (provider_id, model_name, source, favorite)
		 SELECT id, default_model, 'manual', 1 FROM providers WHERE default_model <> ''`)
	return nil
}
