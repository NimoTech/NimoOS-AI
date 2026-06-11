package service

import "time"

// ProviderModel is one model offered by a cloud provider.
type ProviderModel struct {
	ID         int64
	ProviderID int64
	ModelName  string
	Source     string // "fetched" | "manual"
	Favorite   bool
	CreatedAt  time.Time
}

// ProviderModelInput is the per-model payload accepted by ReconcileModels.
// Source is intentionally ignored — DB is the source of truth (anti-tamper).
type ProviderModelInput struct {
	Name     string `json:"name"`
	Favorite bool   `json:"favorite"`
	Source   string `json:"source"` // accepted but ignored
}

// ListModels returns all models for a provider, fetched-then-manual is not
// guaranteed; ordered by id for stability.
func (s *providerService) ListModels(providerID int64) ([]*ProviderModel, error) {
	rows, err := s.db.Query(
		`SELECT id, provider_id, model_name, source, favorite, created_at
		 FROM provider_models WHERE provider_id=? ORDER BY id`, providerID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*ProviderModel
	for rows.Next() {
		m := &ProviderModel{}
		var fav int
		if err := rows.Scan(&m.ID, &m.ProviderID, &m.ModelName, &m.Source, &fav, &m.CreatedAt); err != nil {
			return nil, err
		}
		m.Favorite = fav == 1
		out = append(out, m)
	}
	return out, rows.Err()
}

// ListFavoriteModels returns only favorite=1 models for a provider.
func (s *providerService) ListFavoriteModels(providerID int64) ([]*ProviderModel, error) {
	all, err := s.ListModels(providerID)
	if err != nil {
		return nil, err
	}
	var out []*ProviderModel
	for _, m := range all {
		if m.Favorite {
			out = append(out, m)
		}
	}
	return out, nil
}

// UpsertFetchedModels inserts each name as a fetched row. On conflict it promotes
// an existing manual row to fetched while preserving its favorite flag. Existing
// rows not in names are left untouched.
func (s *providerService) UpsertFetchedModels(providerID int64, names []string) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, n := range names {
		if n == "" {
			continue
		}
		if _, err := tx.Exec(
			`INSERT INTO provider_models (provider_id, model_name, source, favorite)
			 VALUES (?,?, 'fetched', 0)
			 ON CONFLICT(provider_id, model_name) DO UPDATE SET source='fetched'`,
			providerID, n); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// ReconcileModels applies a client PUT. DB is the source of truth for `source`:
//   - fetched rows: only favorite may change; never deleted (omission ignored).
//   - manual rows: added/deleted/favorited per the desired list.
//   - a desired name absent from DB is inserted as a manual row.
// Returns the full reconciled list.
func (s *providerService) ReconcileModels(providerID int64, desired []ProviderModelInput) ([]*ProviderModel, error) {
	existing, err := s.ListModels(providerID)
	if err != nil {
		return nil, err
	}
	existingByName := map[string]*ProviderModel{}
	for _, m := range existing {
		existingByName[m.ModelName] = m
	}
	desiredByName := map[string]ProviderModelInput{}
	for _, d := range desired {
		if d.Name != "" {
			desiredByName[d.Name] = d
		}
	}

	tx, err := s.db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	// Apply desired: update favorite for existing, insert manual for new.
	for name, d := range desiredByName {
		if cur, ok := existingByName[name]; ok {
			if _, err := tx.Exec(
				`UPDATE provider_models SET favorite=? WHERE id=?`, boolToInt(d.Favorite), cur.ID); err != nil {
				return nil, err
			}
		} else {
			if _, err := tx.Exec(
				`INSERT INTO provider_models (provider_id, model_name, source, favorite)
				 VALUES (?,?, 'manual', ?)`, providerID, name, boolToInt(d.Favorite)); err != nil {
				return nil, err
			}
		}
	}
	// Delete manual rows that the client omitted. Fetched omissions are ignored.
	for name, cur := range existingByName {
		if _, kept := desiredByName[name]; kept {
			continue
		}
		if cur.Source == "manual" {
			if _, err := tx.Exec(`DELETE FROM provider_models WHERE id=?`, cur.ID); err != nil {
				return nil, err
			}
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return s.ListModels(providerID)
}
