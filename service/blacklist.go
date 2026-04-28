package service

import (
	"database/sql"
	"errors"
	"strings"
)

type BlacklistEntry struct {
	ID        int64  `json:"id"`
	UserID    string `json:"user_id"`
	Pattern   string `json:"pattern"`
	CreatedAt string `json:"created_at"`
}

var ErrInvalidPattern = errors.New("invalid pattern")

type blacklistService struct {
	db *sql.DB
}

func (s *blacklistService) Create(userID string, pattern string) (int64, error) {
	pattern = strings.TrimSpace(pattern)
	if pattern == "" || len(pattern) > 256 {
		return 0, ErrInvalidPattern
	}
	res, err := s.db.Exec(
		"INSERT INTO hard_blacklist (user_id, pattern) VALUES (?,?)",
		userID, pattern,
	)
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

func (s *blacklistService) List(userID string) ([]BlacklistEntry, error) {
	rows, err := s.db.Query(
		"SELECT id, user_id, pattern, created_at FROM hard_blacklist "+
			"WHERE user_id=? ORDER BY id", userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []BlacklistEntry{}
	for rows.Next() {
		var e BlacklistEntry
		if err := rows.Scan(&e.ID, &e.UserID, &e.Pattern, &e.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// ListPatterns returns just the pattern strings, used by the agent proxy
// when injecting the user-blacklist header for the Python agent.
func (s *blacklistService) ListPatterns(userID string) ([]string, error) {
	rows, err := s.db.Query(
		"SELECT pattern FROM hard_blacklist WHERE user_id=? ORDER BY id", userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var p string
		if err := rows.Scan(&p); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

func (s *blacklistService) Delete(userID string, id int64) error {
	res, err := s.db.Exec(
		"DELETE FROM hard_blacklist WHERE id=? AND user_id=?", id, userID)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return sql.ErrNoRows
	}
	return nil
}
