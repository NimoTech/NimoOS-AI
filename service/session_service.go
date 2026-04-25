package service

import (
	"database/sql"
	"errors"
	"time"
)

var ErrSessionNotFound = errors.New("session not found")

type sessionService struct {
	db *sql.DB
}

func (s *sessionService) ListSessions(userID string) ([]*ChatSession, error) {
	rows, err := s.db.Query(`
		SELECT id, user_id, title, created_at, updated_at
		FROM chat_sessions WHERE user_id = ? ORDER BY updated_at DESC`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sessions []*ChatSession
	for rows.Next() {
		sess := &ChatSession{}
		if err := rows.Scan(&sess.ID, &sess.UserID, &sess.Title, &sess.CreatedAt, &sess.UpdatedAt); err != nil {
			return nil, err
		}
		sessions = append(sessions, sess)
	}
	return sessions, rows.Err()
}

func (s *sessionService) CreateSession(userID, title string) (*ChatSession, error) {
	now := time.Now()
	res, err := s.db.Exec(`
		INSERT INTO chat_sessions (user_id, title, created_at, updated_at)
		VALUES (?, ?, ?, ?)`, userID, title, now, now)
	if err != nil {
		return nil, err
	}
	id, _ := res.LastInsertId()
	return &ChatSession{ID: id, UserID: userID, Title: title, CreatedAt: now, UpdatedAt: now}, nil
}

func (s *sessionService) DeleteSession(userID string, sessionID int64) error {
	res, err := s.db.Exec(`DELETE FROM chat_sessions WHERE id = ? AND user_id = ?`, sessionID, userID)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrSessionNotFound
	}
	return nil
}

func (s *sessionService) ListMessages(userID string, sessionID int64) ([]*ChatMessage, error) {
	var count int
	if err := s.db.QueryRow(`SELECT COUNT(*) FROM chat_sessions WHERE id = ? AND user_id = ?`,
		sessionID, userID).Scan(&count); err != nil {
		return nil, err
	}
	if count == 0 {
		return nil, ErrSessionNotFound
	}
	rows, err := s.db.Query(`
		SELECT id, session_id, role, content, created_at
		FROM chat_messages WHERE session_id = ? ORDER BY id ASC`, sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var msgs []*ChatMessage
	for rows.Next() {
		m := &ChatMessage{}
		if err := rows.Scan(&m.ID, &m.SessionID, &m.Role, &m.Content, &m.CreatedAt); err != nil {
			return nil, err
		}
		msgs = append(msgs, m)
	}
	return msgs, rows.Err()
}

func (s *sessionService) AppendMessages(userID string, sessionID int64, msgs []ChatMessage) error {
	var count int
	if err := s.db.QueryRow(`SELECT COUNT(*) FROM chat_sessions WHERE id = ? AND user_id = ?`,
		sessionID, userID).Scan(&count); err != nil {
		return err
	}
	if count == 0 {
		return ErrSessionNotFound
	}
	now := time.Now()
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, m := range msgs {
		if _, err := tx.Exec(
			`INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)`,
			sessionID, m.Role, m.Content, now,
		); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(`UPDATE chat_sessions SET updated_at = ? WHERE id = ?`, now, sessionID); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *sessionService) UpdateTitle(userID string, sessionID int64, title string) error {
	res, err := s.db.Exec(
		`UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?`,
		title, time.Now(), sessionID, userID,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrSessionNotFound
	}
	return nil
}
