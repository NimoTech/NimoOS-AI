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

	CREATE TABLE IF NOT EXISTS hard_blacklist (
		id         INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id    TEXT NOT NULL,
		pattern    TEXT NOT NULL,
		created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
		UNIQUE(user_id, pattern)
	);
	CREATE INDEX IF NOT EXISTS idx_blacklist_user ON hard_blacklist(user_id);

	CREATE TABLE IF NOT EXISTS user_skills (
		id             TEXT NOT NULL,
		user_id        TEXT NOT NULL,
		name           TEXT NOT NULL,
		title          TEXT NOT NULL DEFAULT '',
		description    TEXT NOT NULL DEFAULT '',
		trigger_kind   TEXT NOT NULL DEFAULT 'auto',
		trigger_human  TEXT NOT NULL DEFAULT '',
		color          TEXT NOT NULL DEFAULT 'blue',
		icon           TEXT NOT NULL DEFAULT 'sparkle',
		enabled        INTEGER NOT NULL DEFAULT 1,
		author         TEXT NOT NULL DEFAULT 'You',
		last_used      TEXT NOT NULL DEFAULT '',
		calls          INTEGER NOT NULL DEFAULT 0,
		files_json     TEXT NOT NULL DEFAULT '[]',
		examples_json  TEXT NOT NULL DEFAULT '[]',
		md             TEXT NOT NULL DEFAULT '',
		created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
	// Idempotent column additions for existing databases
	_, _ = db.Exec(`ALTER TABLE providers ADD COLUMN default_model TEXT NOT NULL DEFAULT ''`)
	_, _ = db.Exec(`ALTER TABLE providers ADD COLUMN provider_type TEXT NOT NULL DEFAULT ''`)

	// One-time backfill: classify rows whose provider_type is still empty.
	rows, err := db.Query(`SELECT id, base_url, protocol FROM providers WHERE provider_type=''`)
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
	return nil
}
