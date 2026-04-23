package service

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/stretchr/testify/require"
)

// ── OllamaChecker tests ──────────────────────────────────────────

func TestOllamaChecker_Healthy(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/tags", r.URL.Path)
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"models":[]}`))
	}))
	defer server.Close()

	checker := NewOllamaChecker(server.URL)
	require.True(t, checker.IsHealthy())
}

func TestOllamaChecker_Unhealthy(t *testing.T) {
	// Port that nothing is listening on
	checker := NewOllamaChecker("http://127.0.0.1:19988")
	require.False(t, checker.IsHealthy())
}

func TestOllamaChecker_AlertAfterConsecutiveFailures(t *testing.T) {
	checker := NewOllamaChecker("http://127.0.0.1:19989")
	checker.maxFailures = 2

	var unhealthyCount int32
	var recoveredCount int32
	checker.onUnhealthy = func() { atomic.AddInt32(&unhealthyCount, 1) }
	checker.onRecovered = func() { atomic.AddInt32(&recoveredCount, 1) }

	checker.check() // failure 1: count = 1, no alert yet
	require.Equal(t, int32(0), atomic.LoadInt32(&unhealthyCount))

	checker.check() // failure 2: count = 2, triggers alert
	require.Equal(t, int32(1), atomic.LoadInt32(&unhealthyCount))

	checker.check() // failure 3: already alerted, no duplicate
	require.Equal(t, int32(1), atomic.LoadInt32(&unhealthyCount))
}

func TestOllamaChecker_RecoveryResetsAlert(t *testing.T) {
	var healthy atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if healthy.Load() {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusInternalServerError)
		}
	}))
	defer server.Close()

	checker := NewOllamaChecker(server.URL)
	checker.maxFailures = 1

	var recovered int32
	checker.onUnhealthy = func() {}
	checker.onRecovered = func() { atomic.AddInt32(&recovered, 1) }

	checker.check() // fail → alert sent
	healthy.Store(true)
	checker.check() // recover
	require.Equal(t, int32(1), atomic.LoadInt32(&recovered))
}

func TestOllamaChecker_SetCallbacks(t *testing.T) {
	checker := NewOllamaChecker("http://127.0.0.1:19990")
	var called int32
	checker.SetCallbacks(func() { atomic.AddInt32(&called, 1) }, func() {})
	require.NotNil(t, checker.onUnhealthy)
}

// ── LocalAdapter tests ───────────────────────────────────────────

func TestLocalAdapter_ForwardsChatCompletions(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/api/chat", r.URL.Path)
		require.Equal(t, "application/json", r.Header.Get("Content-Type"))
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"message":{"role":"assistant","content":"hello"},"done":true}`))
	}))
	defer server.Close()

	adapter := NewLocalAdapter(server.URL)
	body := `{"model":"llama3","messages":[{"role":"user","content":"hi"}],"stream":false}`
	resp, err := adapter.ChatCompletions(strings.NewReader(body))
	require.NoError(t, err)
	require.NotNil(t, resp)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	resp.Body.Close()
}

func TestLocalAdapter_ReturnsErrWhenOllamaDown(t *testing.T) {
	adapter := NewLocalAdapter("http://127.0.0.1:19991")
	body := `{"model":"llama3","messages":[{"role":"user","content":"hi"}]}`
	_, err := adapter.ChatCompletions(strings.NewReader(body))
	require.Error(t, err)
}

func TestLocalAdapter_PassesBodyThrough(t *testing.T) {
	var receivedBody string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		receivedBody = string(data)
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{}`))
	}))
	defer server.Close()

	adapter := NewLocalAdapter(server.URL)
	originalBody := `{"model":"llama3","messages":[],"stream":true}`
	resp, err := adapter.ChatCompletions(strings.NewReader(originalBody))
	require.NoError(t, err)
	resp.Body.Close()
	require.Equal(t, originalBody, receivedBody)
}

