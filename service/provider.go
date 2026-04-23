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

func (s *providerService) ListProviders(userID string) ([]*Provider, error) {
	rows, err := s.db.Query(
		`SELECT id, user_id, name, base_url, api_key, protocol, enabled FROM providers WHERE user_id=? ORDER BY id`,
		userID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var result []*Provider
	for rows.Next() {
		p := &Provider{}
		var enabled int
		var protocol string
		if err := rows.Scan(&p.ID, &p.UserID, &p.Name, &p.BaseURL, &p.APIKey, &protocol, &enabled); err != nil {
			return nil, err
		}
		p.Protocol = Protocol(protocol)
		p.Enabled = enabled == 1
		result = append(result, p)
	}
	return result, rows.Err()
}

func (s *providerService) UpdateProvider(p *Provider) error {
	_, err := s.db.Exec(
		`UPDATE providers SET name=?, base_url=?, api_key=?, protocol=?, enabled=? WHERE id=? AND user_id=?`,
		p.Name, p.BaseURL, p.APIKey, string(p.Protocol), boolToInt(p.Enabled), p.ID, p.UserID,
	)
	return err
}

func (s *providerService) DeleteProvider(id int64, userID string) error {
	_, err := s.db.Exec(`DELETE FROM providers WHERE id=? AND user_id=?`, id, userID)
	return err
}

func (s *providerService) UpsertPolicy(p *PrivacyPolicy) error {
	_, err := s.db.Exec(
		`INSERT INTO privacy_policies (user_id, allow_remote, default_backend, escalation_prompt)
         VALUES (?,?,?,?)
         ON CONFLICT(user_id) DO UPDATE SET
           allow_remote=excluded.allow_remote,
           default_backend=excluded.default_backend,
           escalation_prompt=excluded.escalation_prompt`,
		p.UserID, boolToInt(p.AllowRemote), p.DefaultBackend, boolToInt(p.EscalationPrompt),
	)
	return err
}

func (s *providerService) GetPolicy(userID string) (*PrivacyPolicy, error) {
	p := &PrivacyPolicy{}
	var allowRemote, escalation int
	row := s.db.QueryRow(
		`SELECT user_id, allow_remote, default_backend, escalation_prompt FROM privacy_policies WHERE user_id=?`,
		userID,
	)
	err := row.Scan(&p.UserID, &allowRemote, &p.DefaultBackend, &escalation)
	if err != nil {
		return nil, err
	}
	p.AllowRemote = allowRemote == 1
	p.EscalationPrompt = escalation == 1
	return p, nil
}
