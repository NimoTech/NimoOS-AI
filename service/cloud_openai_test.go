package service

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestOpenAIAdapter_ForwardsRequest(t *testing.T) {
	var gotAuthHeader, gotPath, gotContentType string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuthHeader = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		gotContentType = r.Header.Get("Content-Type")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"choices":[{"message":{"role":"assistant","content":"hi"}}]}`))
	}))
	defer server.Close()

	adapter := NewOpenAIAdapter(server.URL, "sk-test123")
	body := `{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}`
	resp, err := adapter.ChatCompletions(strings.NewReader(body))
	require.NoError(t, err)
	defer resp.Body.Close()

	require.Equal(t, "Bearer sk-test123", gotAuthHeader)
	require.Equal(t, "/v1/chat/completions", gotPath)
	require.Equal(t, "application/json", gotContentType)
	require.Equal(t, http.StatusOK, resp.StatusCode)
}

func TestOpenAIAdapter_PassesBodyUnchanged(t *testing.T) {
	var receivedBody string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		receivedBody = string(data)
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{}`))
	}))
	defer server.Close()

	adapter := NewOpenAIAdapter(server.URL, "key")
	originalBody := `{"model":"gpt-4o","messages":[],"stream":true}`
	resp, err := adapter.ChatCompletions(strings.NewReader(originalBody))
	require.NoError(t, err)
	resp.Body.Close()
	require.Equal(t, originalBody, receivedBody)
}

func TestOpenAIAdapter_ReturnsUpstreamError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		w.Write([]byte(`{"error":{"message":"Invalid API key"}}`))
	}))
	defer server.Close()

	adapter := NewOpenAIAdapter(server.URL, "bad-key")
	body := `{"model":"gpt-4o","messages":[]}`
	resp, err := adapter.ChatCompletions(strings.NewReader(body))
	// Network succeeds, HTTP error is returned as status code (caller handles it)
	require.NoError(t, err)
	defer resp.Body.Close()
	require.Equal(t, http.StatusUnauthorized, resp.StatusCode)
}

func TestOpenAIAdapter_ConnectionRefused(t *testing.T) {
	adapter := NewOpenAIAdapter("http://127.0.0.1:19992", "key")
	_, err := adapter.ChatCompletions(strings.NewReader(`{}`))
	require.Error(t, err)
}
