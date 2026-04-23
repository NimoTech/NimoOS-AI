package service

import "database/sql"

type providerService struct{ db *sql.DB }

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// CreateProvider inserts a new provider and sets p.ID
func (s *providerService) CreateProvider(p *Provider) error {
	res, err := s.db.Exec(
		`INSERT INTO providers (user_id, name, base_url, api_key, protocol, enabled) VALUES (?,?,?,?,?,?)`,
		p.UserID, p.Name, p.BaseURL, p.APIKey, string(p.Protocol), boolToInt(p.Enabled),
	)
	if err != nil {
		return err
	}
	p.ID, _ = res.LastInsertId()
	return nil
}

// GetProvider retrieves a provider by ID, scoped to userID
func (s *providerService) GetProvider(id int64, userID string) (*Provider, error) {
	p := &Provider{}
	var enabled int
	var protocol string
	row := s.db.QueryRow(
		`SELECT id, user_id, name, base_url, api_key, protocol, enabled FROM providers WHERE id=? AND user_id=?`,
		id, userID,
	)
	err := row.Scan(&p.ID, &p.UserID, &p.Name, &p.BaseURL, &p.APIKey, &protocol, &enabled)
	if err != nil {
		return nil, err
	}
	p.Protocol = Protocol(protocol)
	p.Enabled = enabled == 1
	return p, nil
}
