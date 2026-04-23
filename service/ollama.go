package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"sync/atomic"
	"time"
)

// ── OllamaChecker ────────────────────────────────────────────────

// OllamaChecker polls Ollama health and fires callbacks on state changes.
// Ollama is managed by systemd; this checker only monitors, it does not manage the process.
type OllamaChecker struct {
	baseURL     string
	client      *http.Client
	failures    int32
	maxFailures int32
	alertSent   atomic.Bool
	onUnhealthy func()
	onRecovered func()
}

func NewOllamaChecker(baseURL string) *OllamaChecker {
	return &OllamaChecker{
		baseURL:     baseURL,
		client:      &http.Client{Timeout: 3 * time.Second},
		maxFailures: 3,
		onUnhealthy: func() {},
		onRecovered: func() {},
	}
}

// SetCallbacks replaces the default no-op callbacks.
func (o *OllamaChecker) SetCallbacks(onUnhealthy, onRecovered func()) {
	o.onUnhealthy = onUnhealthy
	o.onRecovered = onRecovered
}

// IsHealthy returns true if Ollama responds to GET /api/tags with 200.
func (o *OllamaChecker) IsHealthy() bool {
	resp, err := o.client.Get(o.baseURL + "/api/tags")
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// check runs one health probe and fires callbacks on threshold transitions.
func (o *OllamaChecker) check() {
	if o.IsHealthy() {
		if o.alertSent.Swap(false) {
			o.onRecovered()
		}
		atomic.StoreInt32(&o.failures, 0)
		return
	}
	count := atomic.AddInt32(&o.failures, 1)
	if count >= o.maxFailures && !o.alertSent.Swap(true) {
		o.onUnhealthy()
	}
}

// Start polls Ollama every 30 seconds until ctx is cancelled.
func (o *OllamaChecker) Start(ctx context.Context) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			o.check()
		}
	}
}

// ── LocalAdapter ─────────────────────────────────────────────────

// LocalAdapter proxies LLM requests to a local Ollama instance.
type LocalAdapter struct {
	baseURL string
	client  *http.Client
}

func NewLocalAdapter(baseURL string) *LocalAdapter {
	return &LocalAdapter{
		baseURL: baseURL,
		client:  &http.Client{}, // no timeout: streaming responses can be long
	}
}

// ChatCompletions forwards the request body to Ollama /api/chat and returns the raw response.
// The caller is responsible for reading and closing resp.Body.
func (l *LocalAdapter) ChatCompletions(body io.Reader) (*http.Response, error) {
	data, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(http.MethodPost, l.baseURL+"/api/chat", bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	return l.client.Do(req)
}
