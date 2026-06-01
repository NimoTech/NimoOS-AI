package service

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
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

// ChatCompletions forwards the request body to Ollama /v1/chat/completions (OpenAI-compatible
// endpoint) so the response is already in OpenAI SSE format and can be proxied directly.
// The caller is responsible for reading and closing resp.Body.
func (l *LocalAdapter) ChatCompletions(body io.Reader) (*http.Response, error) {
	data, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequest(http.MethodPost, l.baseURL+"/v1/chat/completions", bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	return l.client.Do(req)
}

// injectThinkFalse appends "/no_think" to the last user message to disable Qwen3
// thinking mode. Qwen3 (and compatible models) look for this token at the end of
// the user turn to suppress the internal reasoning phase. Ollama's OpenAI-compatible
// endpoint does not propagate options.think to template variables, so direct message
// modification is the only reliable approach.
//
// The injection is skipped if the last user message already ends with "/think" or
// "/no_think" (caller opted in explicitly).
func injectThinkFalse(body []byte) []byte {
	var req map[string]json.RawMessage
	if err := json.Unmarshal(body, &req); err != nil {
		return body
	}

	messagesRaw, ok := req["messages"]
	if !ok {
		return body
	}
	var messages []map[string]json.RawMessage
	if err := json.Unmarshal(messagesRaw, &messages); err != nil {
		return body
	}

	// Find the last user message and append /no_think if not already set.
	for i := len(messages) - 1; i >= 0; i-- {
		var role string
		if err := json.Unmarshal(messages[i]["role"], &role); err != nil || role != "user" {
			continue
		}
		contentRaw, hasContent := messages[i]["content"]
		if !hasContent {
			break
		}
		var content string
		if err := json.Unmarshal(contentRaw, &content); err != nil {
			break
		}
		// Respect explicit caller intent.
		if strings.HasSuffix(strings.TrimSpace(content), "/think") ||
			strings.HasSuffix(strings.TrimSpace(content), "/no_think") {
			break
		}
		encoded, err := json.Marshal(content + " /no_think")
		if err != nil {
			break
		}
		messages[i]["content"] = encoded
		messagesEncoded, err := json.Marshal(messages)
		if err != nil {
			break
		}
		req["messages"] = messagesEncoded
		break
	}

	out, err := json.Marshal(req)
	if err != nil {
		return body
	}
	return out
}
