package service

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestFetchModels_OpenAI(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/models", r.URL.Path)
		require.Equal(t, "Bearer sk-test", r.Header.Get("Authorization"))
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"data":[{"id":"gpt-4o"},{"id":"o3"}]}`))
	}))
	defer srv.Close()

	p := &Provider{BaseURL: srv.URL, Protocol: ProtocolOpenAI, ProviderType: "openai"}
	models, err := FetchModels(p, "sk-test")
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-4o", "o3"}, models)
}

func TestFetchModels_Anthropic(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/models", r.URL.Path)
		require.Equal(t, "sk-ant", r.Header.Get("x-api-key"))
		require.NotEmpty(t, r.Header.Get("anthropic-version"))
		w.Write([]byte(`{"data":[{"id":"claude-sonnet-4-6"}]}`))
	}))
	defer srv.Close()

	p := &Provider{BaseURL: srv.URL, Protocol: ProtocolAnthropic, ProviderType: "anthropic"}
	models, err := FetchModels(p, "sk-ant")
	require.NoError(t, err)
	require.Equal(t, []string{"claude-sonnet-4-6"}, models)
}

func TestFetchModels_Ollama_UsesTags(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/tags", r.URL.Path)
		w.Write([]byte(`{"models":[{"name":"llama3"},{"name":"qwen3"}]}`))
	}))
	defer srv.Close()

	// Ollama base_url includes the /v1 suffix; FetchModels strips it for /api/tags.
	p := &Provider{BaseURL: srv.URL + "/v1", Protocol: ProtocolOpenAI, ProviderType: "ollama"}
	models, err := FetchModels(p, "")
	require.NoError(t, err)
	require.Equal(t, []string{"llama3", "qwen3"}, models)
}

func TestFetchModels_UpstreamError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	p := &Provider{BaseURL: srv.URL, Protocol: ProtocolOpenAI, ProviderType: "openai"}
	_, err := FetchModels(p, "bad")
	require.Error(t, err)
}
