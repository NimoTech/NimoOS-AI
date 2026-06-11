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

func newTestProviderSvc(t *testing.T) (*providerService, int64) {
	t.Helper()
	db, err := NewDB(t.TempDir() + "/ai.db")
	require.NoError(t, err)
	t.Cleanup(func() { db.Close() })
	svc := &providerService{db: db}
	p := &Provider{UserID: "10", Name: "DS", BaseURL: "https://api.deepseek.com/v1", Protocol: ProtocolOpenAI, Enabled: true}
	require.NoError(t, svc.CreateProvider(p))
	return svc, p.ID
}

func TestUpsertFetchedModels_InsertsAndPromotes(t *testing.T) {
	svc, pid := newTestProviderSvc(t)

	// Seed a manual favorite model first.
	_, err := svc.db.Exec(
		`INSERT INTO provider_models (provider_id, model_name, source, favorite) VALUES (?,?,?,?)`,
		pid, "deepseek-chat", "manual", 1)
	require.NoError(t, err)

	// Fetch returns deepseek-chat (already manual) + a new one.
	require.NoError(t, svc.UpsertFetchedModels(pid, []string{"deepseek-chat", "deepseek-reasoner"}))

	models, err := svc.ListModels(pid)
	require.NoError(t, err)
	require.Len(t, models, 2)

	byName := map[string]*ProviderModel{}
	for _, m := range models {
		byName[m.ModelName] = m
	}
	// manual model promoted to fetched, favorite preserved.
	require.Equal(t, "fetched", byName["deepseek-chat"].Source)
	require.True(t, byName["deepseek-chat"].Favorite)
	// new model inserted as fetched, not favorite.
	require.Equal(t, "fetched", byName["deepseek-reasoner"].Source)
	require.False(t, byName["deepseek-reasoner"].Favorite)
}

func TestReconcileModels_TamperProof(t *testing.T) {
	svc, pid := newTestProviderSvc(t)
	require.NoError(t, svc.UpsertFetchedModels(pid, []string{"gpt-4o", "whisper-1"}))

	// Client tries to: favorite gpt-4o, relabel it 'manual' (must be ignored),
	// drop whisper-1 (a fetched row → deletion must be ignored), add a manual model.
	out, err := svc.ReconcileModels(pid, []ProviderModelInput{
		{Name: "gpt-4o", Favorite: true, Source: "manual"},
		{Name: "my-custom", Favorite: true, Source: "fetched"},
	})
	require.NoError(t, err)

	byName := map[string]*ProviderModel{}
	for _, m := range out {
		byName[m.ModelName] = m
	}
	require.Equal(t, "fetched", byName["gpt-4o"].Source, "source is read-only, client cannot relabel")
	require.True(t, byName["gpt-4o"].Favorite)
	require.Contains(t, byName, "whisper-1", "fetched row not deleted even when omitted")
	require.Equal(t, "manual", byName["my-custom"].Source, "new name added as manual regardless of client source")
	require.True(t, byName["my-custom"].Favorite)
}

func TestReconcileModels_DeletesManualWhenOmitted(t *testing.T) {
	svc, pid := newTestProviderSvc(t)
	_, err := svc.db.Exec(
		`INSERT INTO provider_models (provider_id, model_name, source, favorite) VALUES (?,?,?,?)`,
		pid, "old-manual", "manual", 1)
	require.NoError(t, err)

	out, err := svc.ReconcileModels(pid, []ProviderModelInput{}) // empty desired
	require.NoError(t, err)
	require.Empty(t, out, "omitted manual row should be deleted")
}

func TestListFavoriteModels(t *testing.T) {
	svc, pid := newTestProviderSvc(t)
	require.NoError(t, svc.UpsertFetchedModels(pid, []string{"a", "b", "c"}))
	_, err := svc.ReconcileModels(pid, []ProviderModelInput{
		{Name: "a", Favorite: true},
		{Name: "b", Favorite: false},
	})
	require.NoError(t, err)

	favs, err := svc.ListFavoriteModels(pid)
	require.NoError(t, err)
	require.Len(t, favs, 1)
	require.Equal(t, "a", favs[0].ModelName)
}
