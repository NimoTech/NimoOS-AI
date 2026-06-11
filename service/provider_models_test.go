package service

import (
	"testing"

	_ "github.com/mattn/go-sqlite3"
	"github.com/stretchr/testify/require"
)

func TestMigrate_CreatesProviderModelsTable(t *testing.T) {
	db, err := NewDB(t.TempDir() + "/ai.db")
	require.NoError(t, err)
	defer db.Close()

	// Table exists and has the expected columns.
	rows, err := db.Query(`PRAGMA table_info(provider_models)`)
	require.NoError(t, err)
	defer rows.Close()
	cols := map[string]bool{}
	for rows.Next() {
		var cid, nn, pk int
		var name, ctype string
		var dflt interface{}
		require.NoError(t, rows.Scan(&cid, &name, &ctype, &nn, &dflt, &pk))
		cols[name] = true
	}
	for _, want := range []string{"id", "provider_id", "model_name", "source", "favorite", "created_at"} {
		require.True(t, cols[want], "missing column %s", want)
	}
}

func TestMigrate_BackfillsDefaultModelAsFavorite(t *testing.T) {
	db, err := NewDB(t.TempDir() + "/ai.db")
	require.NoError(t, err)
	defer db.Close()

	// Seed a legacy provider row with a default_model.
	res, err := db.Exec(
		`INSERT INTO providers (user_id, name, base_url, protocol, enabled, default_model, provider_type)
		 VALUES ('10','DS','https://api.deepseek.com/v1','openai',1,'deepseek-chat','deepseek')`)
	require.NoError(t, err)
	pid, _ := res.LastInsertId()

	// Re-run migrate to trigger backfill (idempotent).
	require.NoError(t, migrate(db))

	var name, source string
	var fav int
	err = db.QueryRow(
		`SELECT model_name, source, favorite FROM provider_models WHERE provider_id=?`, pid,
	).Scan(&name, &source, &fav)
	require.NoError(t, err)
	require.Equal(t, "deepseek-chat", name)
	require.Equal(t, "manual", source)
	require.Equal(t, 1, fav)
}
