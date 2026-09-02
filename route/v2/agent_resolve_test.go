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

// A provider saved without an API key (self-hosted llama.cpp / vLLM / OVMS
// behind an OpenAI-compatible URL) must still resolve. Before the fix the
// empty ciphertext failed MasterKey.Decrypt ("ciphertext too short"), so the
// explicit X-Agent-Provider-Id was silently ignored and the request fell back
// to the first enabled provider — a different vendor — or to no headers at all.
func TestResolveProvider_EmptyKeyMeansNoAuth(t *testing.T) {
	svc := newTestServices(t)
	p := &service.Provider{UserID: "11", Name: "llama.cpp", BaseURL: "http://192.168.1.183:8081/v1", Protocol: service.ProtocolOpenAI, Enabled: true, APIKey: ""}
	require.NoError(t, svc.Providers().CreateProvider(p))

	h := &AgentHandler{svc: svc}

	key, url, ok := h.resolveProviderByID("11", p.ID)
	require.True(t, ok, "no-key provider must resolve by id")
	require.Equal(t, noAuthAPIKey, key, "placeholder key so the agent's OpenAI SDK accepts it")
	require.Equal(t, "http://192.168.1.183:8081/v1", url)

	key, url, ok = h.resolveProvider("11")
	require.True(t, ok, "no-key provider must resolve as first-enabled fallback")
	require.Equal(t, noAuthAPIKey, key)
	require.Equal(t, "http://192.168.1.183:8081/v1", url)
}
