package v2

import (
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/stretchr/testify/require"
)

func TestResolveProviderByID(t *testing.T) {
	svc := newTestServices(t)
	enc, err := svc.MasterKey().Encrypt("sk-secret")
	require.NoError(t, err)
	p := &service.Provider{UserID: "10", Name: "OAI", BaseURL: "https://api.openai.com/v1", Protocol: service.ProtocolOpenAI, Enabled: true, APIKey: enc}
	require.NoError(t, svc.Providers().CreateProvider(p))

	h := &AgentHandler{svc: svc}
	key, url, ok := h.resolveProviderByID("10", p.ID)
	require.True(t, ok)
	require.Equal(t, "sk-secret", key)
	require.Equal(t, "https://api.openai.com/v1", url)

	// disabled provider → not ok
	p2 := &service.Provider{UserID: "10", Name: "D", BaseURL: "x", Protocol: service.ProtocolOpenAI, Enabled: false, APIKey: enc}
	require.NoError(t, svc.Providers().CreateProvider(p2))
	_, _, ok2 := h.resolveProviderByID("10", p2.ID)
	require.False(t, ok2)
}
