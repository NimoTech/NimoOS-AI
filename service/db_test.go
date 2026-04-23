package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestDBInit_CreatesAllTables(t *testing.T) {
	dbPath := t.TempDir() + "/test.db"
	db, err := NewDB(dbPath)
	require.NoError(t, err)
	defer db.Close()

	tables := []string{"providers", "privacy_policies", "chat_sessions", "chat_messages"}
	for _, table := range tables {
		var name string
		row := db.QueryRow("SELECT name FROM sqlite_master WHERE type='table' AND name=?", table)
		err := row.Scan(&name)
		require.NoError(t, err, "table %s should exist", table)
	}
}

func TestDBInit_Idempotent(t *testing.T) {
	dbPath := t.TempDir() + "/test.db"
	// Open twice - migrate should be idempotent (CREATE TABLE IF NOT EXISTS)
	db1, err := NewDB(dbPath)
	require.NoError(t, err)
	db1.Close()

	db2, err := NewDB(dbPath)
	require.NoError(t, err)
	defer db2.Close()
}

func TestProviderCRUD(t *testing.T) {
	db, err := NewDB(t.TempDir() + "/test.db")
	require.NoError(t, err)
	defer db.Close()

	svc := &providerService{db: db}

	p := &Provider{
		UserID:   "42",
		Name:     "OpenAI",
		BaseURL:  "https://api.openai.com",
		APIKey:   "encrypted-key",
		Protocol: ProtocolOpenAI,
		Enabled:  true,
	}
	err = svc.CreateProvider(p)
	require.NoError(t, err)
	require.NotZero(t, p.ID)

	got, err := svc.GetProvider(p.ID, "42")
	require.NoError(t, err)
	require.Equal(t, "OpenAI", got.Name)
	require.Equal(t, ProtocolOpenAI, got.Protocol)
	require.True(t, got.Enabled)
}

func TestProvider_UserIDIsolation(t *testing.T) {
	db, _ := NewDB(t.TempDir() + "/test.db")
	defer db.Close()
	svc := &providerService{db: db}

	p := &Provider{UserID: "1", Name: "Test", BaseURL: "https://test.com", Protocol: ProtocolOpenAI, Enabled: true}
	svc.CreateProvider(p)

	// user "2" should not be able to get user "1"'s provider
	_, err := svc.GetProvider(p.ID, "2")
	require.Error(t, err) // should be sql.ErrNoRows
}

func TestForeignKeyConstraint(t *testing.T) {
	db, _ := NewDB(t.TempDir() + "/test.db")
	defer db.Close()

	// Try to insert a message with non-existent session_id - should fail due to FK constraint
	_, err := db.Exec(`INSERT INTO chat_messages (session_id, role, content) VALUES (9999, 'user', 'hi')`)
	require.Error(t, err, "foreign key constraint should be enforced")
}

func TestPrivacyPolicyCRUD(t *testing.T) {
	db, err := NewDB(t.TempDir() + "/test.db")
	require.NoError(t, err)
	svc := &providerService{db: db}

	policy := &PrivacyPolicy{UserID: "5", AllowRemote: true, DefaultBackend: "local", EscalationPrompt: true}
	err = svc.UpsertPolicy(policy)
	require.NoError(t, err)

	got, err := svc.GetPolicy("5")
	require.NoError(t, err)
	require.True(t, got.AllowRemote)

	policy.AllowRemote = false
	err = svc.UpsertPolicy(policy)
	require.NoError(t, err)

	got2, err := svc.GetPolicy("5")
	require.NoError(t, err)
	require.False(t, got2.AllowRemote)
}

func TestProviderListAndDelete(t *testing.T) {
	db, err := NewDB(t.TempDir() + "/test.db")
	require.NoError(t, err)
	svc := &providerService{db: db}

	err = svc.CreateProvider(&Provider{UserID: "10", Name: "A", BaseURL: "https://a.com", Protocol: ProtocolOpenAI})
	require.NoError(t, err)
	err = svc.CreateProvider(&Provider{UserID: "10", Name: "B", BaseURL: "https://b.com", Protocol: ProtocolOpenAI})
	require.NoError(t, err)
	err = svc.CreateProvider(&Provider{UserID: "99", Name: "C", BaseURL: "https://c.com", Protocol: ProtocolOpenAI})
	require.NoError(t, err)

	list, err := svc.ListProviders("10")
	require.NoError(t, err)
	require.Len(t, list, 2)

	err = svc.DeleteProvider(list[0].ID, "10")
	require.NoError(t, err)

	list2, err := svc.ListProviders("10")
	require.NoError(t, err)
	require.Len(t, list2, 1)
}
