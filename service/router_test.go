package service

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func newTestRouter(t *testing.T, policy PrivacyPolicy) *Router {
	db, err := NewDB(t.TempDir() + "/test.db")
	require.NoError(t, err)
	svc := &providerService{db: db}
	_, err = db.Exec(
		`INSERT INTO privacy_policies (user_id, allow_remote, default_backend, escalation_prompt) VALUES (?,?,?,?)`,
		policy.UserID, boolToInt(policy.AllowRemote), policy.DefaultBackend, boolToInt(policy.EscalationPrompt),
	)
	require.NoError(t, err)
	return &Router{providers: svc, db: db}
}

func TestRouter_LocalOnlyPolicy_BlocksCloud(t *testing.T) {
	r := newTestRouter(t, PrivacyPolicy{
		UserID: "1", AllowRemote: false, DefaultBackend: "local",
	})
	decision, err := r.Decide("1", false)
	require.NoError(t, err)
	require.Equal(t, BackendLocal, decision.Backend)
	require.True(t, decision.ForceLocal)
}

func TestRouter_ForceCloudHeader_RespectedWhenAllowed(t *testing.T) {
	r := newTestRouter(t, PrivacyPolicy{
		UserID: "2", AllowRemote: true, DefaultBackend: "local",
	})
	decision, err := r.Decide("2", true)
	require.NoError(t, err)
	require.Equal(t, BackendCloud, decision.Backend)
}

func TestRouter_ForceCloudHeader_BlockedWhenNotAllowed(t *testing.T) {
	r := newTestRouter(t, PrivacyPolicy{
		UserID: "3", AllowRemote: false, DefaultBackend: "local",
	})
	_, err := r.Decide("3", true)
	require.ErrorIs(t, err, ErrRemoteNotAllowed)
}

func TestRouter_DefaultsToLocalForNewUser(t *testing.T) {
	db, err := NewDB(t.TempDir() + "/test.db")
	require.NoError(t, err)
	r := &Router{providers: &providerService{db: db}, db: db}
	decision, err := r.Decide("999", false) // no policy record exists
	require.NoError(t, err)
	require.Equal(t, BackendLocal, decision.Backend)
	require.False(t, decision.ForceLocal) // AllowRemote=true by default, so not force-local
}
