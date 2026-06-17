package service

import (
	"database/sql"
	"time"
)

// McpServer is a user-configured MCP server. Headers/Env hold AES-GCM
// ciphertext (encrypted in the handler layer, mirroring providers.api_key).
type McpServer struct {
	ID        int64
	UserID    string
	Name      string
	Transport string // "http" | "sse" | "stdio"
	URL       string
	Command   string
	Args      string // JSON array string
	Env       string // JSON object string (encrypted)
	Headers   string // JSON object string (encrypted)
	Enabled   bool
	CreatedAt int64
	UpdatedAt int64
}

type mcpService struct{ db *sql.DB }

func (s *mcpService) CreateMcpServer(m *McpServer) error {
	now := time.Now().Unix()
	m.CreatedAt, m.UpdatedAt = now, now
	res, err := s.db.Exec(
		`INSERT INTO mcp_servers
		 (user_id, name, transport, url, command, args, env, headers, enabled, created_at, updated_at)
		 VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
		m.UserID, m.Name, m.Transport, m.URL, m.Command, m.Args, m.Env, m.Headers,
		boolToInt(m.Enabled), m.CreatedAt, m.UpdatedAt,
	)
	if err != nil {
		return err
	}
	m.ID, _ = res.LastInsertId()
	return nil
}

func scanMcp(sc interface{ Scan(...any) error }) (*McpServer, error) {
	m := &McpServer{}
	var enabled int
	if err := sc.Scan(&m.ID, &m.UserID, &m.Name, &m.Transport, &m.URL, &m.Command,
		&m.Args, &m.Env, &m.Headers, &enabled, &m.CreatedAt, &m.UpdatedAt); err != nil {
		return nil, err
	}
	m.Enabled = enabled == 1
	return m, nil
}

const mcpCols = `id, user_id, name, transport, url, command, args, env, headers, enabled, created_at, updated_at`

func (s *mcpService) GetMcpServer(id int64, userID string) (*McpServer, error) {
	row := s.db.QueryRow(`SELECT `+mcpCols+` FROM mcp_servers WHERE id=? AND user_id=?`, id, userID)
	return scanMcp(row)
}

func (s *mcpService) ListMcpServers(userID string) ([]*McpServer, error) {
	return s.queryMcp(`SELECT `+mcpCols+` FROM mcp_servers WHERE user_id=? ORDER BY id`, userID)
}

func (s *mcpService) ListEnabledMcpServers(userID string) ([]*McpServer, error) {
	return s.queryMcp(`SELECT `+mcpCols+` FROM mcp_servers WHERE user_id=? AND enabled=1 ORDER BY id`, userID)
}

func (s *mcpService) queryMcp(q string, args ...any) ([]*McpServer, error) {
	rows, err := s.db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*McpServer
	for rows.Next() {
		m, err := scanMcp(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

func (s *mcpService) UpdateMcpServer(m *McpServer) error {
	m.UpdatedAt = time.Now().Unix()
	res, err := s.db.Exec(
		`UPDATE mcp_servers SET name=?, transport=?, url=?, command=?, args=?, env=?, headers=?, enabled=?, updated_at=?
		 WHERE id=? AND user_id=?`,
		m.Name, m.Transport, m.URL, m.Command, m.Args, m.Env, m.Headers,
		boolToInt(m.Enabled), m.UpdatedAt, m.ID, m.UserID,
	)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return sql.ErrNoRows
	}
	return nil
}

func (s *mcpService) DeleteMcpServer(id int64, userID string) error {
	res, err := s.db.Exec(`DELETE FROM mcp_servers WHERE id=? AND user_id=?`, id, userID)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return sql.ErrNoRows
	}
	return nil
}
