package service

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// ── Protocol conversion tests ──────────────────────────────────

func TestConvertToAnthropic_BasicMessage(t *testing.T) {
	req := OpenAIChatRequest{
		Model: "claude-3-5-sonnet-20241022",
		Messages: []OpenAIMessage{
			{Role: "user", Content: "hello"},
		},
	}
	got := convertToAnthropic(req)
	require.Equal(t, "claude-3-5-sonnet-20241022", got.Model)
	require.Len(t, got.Messages, 1)
	require.Equal(t, "user", got.Messages[0].Role)
	require.Equal(t, "", got.System)
	require.Equal(t, 4096, got.MaxTokens) // default
}

func TestConvertToAnthropic_ExtractsSystemPrompt(t *testing.T) {
	req := OpenAIChatRequest{
		Model: "claude-3-5-sonnet-20241022",
		Messages: []OpenAIMessage{
			{Role: "system", Content: "You are a helpful assistant."},
			{Role: "user", Content: "hello"},
			{Role: "assistant", Content: "hi there"},
		},
	}
	got := convertToAnthropic(req)
	require.Equal(t, "You are a helpful assistant.", got.System)
	require.Len(t, got.Messages, 2) // system extracted
	require.Equal(t, "user", got.Messages[0].Role)
	require.Equal(t, "assistant", got.Messages[1].Role)
}

func TestConvertToAnthropic_RespectsMaxTokens(t *testing.T) {
	req := OpenAIChatRequest{
		Model:     "claude-3-5-sonnet-20241022",
		Messages:  []OpenAIMessage{{Role: "user", Content: "hi"}},
		MaxTokens: 512,
	}
	got := convertToAnthropic(req)
	require.Equal(t, 512, got.MaxTokens)
}

func TestConvertAnthropicChunkToOpenAI_TextDelta(t *testing.T) {
	data := `{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello world"}}`
	chunk, ok := ConvertAnthropicChunkToOpenAI([]byte(data), "claude-3-5-sonnet-20241022")
	require.True(t, ok)
	require.Equal(t, "hello world", chunk.Choices[0].Delta.Content)
	require.Equal(t, 0, chunk.Choices[0].Index)
	require.Nil(t, chunk.Choices[0].FinishReason)
}

func TestConvertAnthropicChunkToOpenAI_MessageStop(t *testing.T) {
	data := `{"type":"message_stop"}`
	chunk, ok := ConvertAnthropicChunkToOpenAI([]byte(data), "claude-3-5-sonnet-20241022")
	require.True(t, ok)
	require.NotNil(t, chunk.Choices[0].FinishReason)
	require.Equal(t, "stop", *chunk.Choices[0].FinishReason)
}

func TestConvertAnthropicChunkToOpenAI_IgnoresPing(t *testing.T) {
	data := `{"type":"ping"}`
	_, ok := ConvertAnthropicChunkToOpenAI([]byte(data), "model")
	require.False(t, ok)
}

func TestConvertAnthropicChunkToOpenAI_IgnoresMessageStart(t *testing.T) {
	data := `{"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant"}}`
	_, ok := ConvertAnthropicChunkToOpenAI([]byte(data), "model")
	require.False(t, ok)
}

func TestConvertAnthropicChunkToOpenAI_IgnoresNonTextDelta(t *testing.T) {
	// input_json_delta for tool use - should be ignored
	data := `{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{}"}}`
	_, ok := ConvertAnthropicChunkToOpenAI([]byte(data), "model")
	require.False(t, ok)
}

func TestConvertAnthropicChunkToOpenAI_ThinkingDelta(t *testing.T) {
	data := `{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","text":"let me think"}}`
	chunk, ok := ConvertAnthropicChunkToOpenAI([]byte(data), "claude-3-7-sonnet-20250219")
	require.True(t, ok)
	require.Equal(t, "let me think", chunk.Choices[0].Delta.ReasoningContent)
	require.Equal(t, "", chunk.Choices[0].Delta.Content)
	require.Equal(t, 0, chunk.Choices[0].Index)
}

// ── AnthropicAdapter tests ─────────────────────────────────────

func TestAnthropicAdapter_ChatCompletions_SetsHeaders(t *testing.T) {
	var gotAPIKey, gotVersion, gotContentType string
	var gotPath string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAPIKey = r.Header.Get("x-api-key")
		gotVersion = r.Header.Get("anthropic-version")
		gotContentType = r.Header.Get("Content-Type")
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{}`))
	}))
	defer server.Close()

	adapter := NewAnthropicAdapter(server.URL, "my-api-key")
	body := `{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"hi"}]}`
	resp, err := adapter.ChatCompletions(strings.NewReader(body))
	require.NoError(t, err)
	resp.Body.Close()

	require.Equal(t, "my-api-key", gotAPIKey)
	require.Equal(t, "2023-06-01", gotVersion)
	require.Equal(t, "application/json", gotContentType)
	require.Equal(t, "/v1/messages", gotPath)
}

func TestAnthropicAdapter_ChatCompletions_ConvertsSystemMessage(t *testing.T) {
	var receivedBody []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{}`))
	}))
	defer server.Close()

	adapter := NewAnthropicAdapter(server.URL, "key")
	openaiBody := `{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"system","content":"be helpful"},{"role":"user","content":"hi"}]}`
	resp, err := adapter.ChatCompletions(strings.NewReader(openaiBody))
	require.NoError(t, err)
	resp.Body.Close()

	var anthropicReq AnthropicRequest
	require.NoError(t, json.Unmarshal(receivedBody, &anthropicReq))
	require.Equal(t, "be helpful", anthropicReq.System)
	require.Len(t, anthropicReq.Messages, 1) // system extracted
}

func TestAnthropicAdapter_ChatCompletions_InvalidJSON(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	adapter := NewAnthropicAdapter(server.URL, "key")
	_, err := adapter.ChatCompletions(strings.NewReader("not-json"))
	require.Error(t, err)
}

func TestAnthropicAdapter_ChatCompletions_ConnectionRefused(t *testing.T) {
	adapter := NewAnthropicAdapter("http://127.0.0.1:1", "key")
	body := `{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"hi"}]}`
	_, err := adapter.ChatCompletions(strings.NewReader(body))
	require.Error(t, err)
}

func TestAnthropicAdapter_Messages_PassesThrough(t *testing.T) {
	var receivedBody string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, _ := io.ReadAll(r.Body)
		receivedBody = string(data)
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{}`))
	}))
	defer server.Close()

	adapter := NewAnthropicAdapter(server.URL, "key")
	originalBody := `{"model":"claude-3-5-sonnet-20241022","messages":[{"role":"user","content":"hi"}],"max_tokens":1024}`
	resp, err := adapter.Messages(strings.NewReader(originalBody))
	require.NoError(t, err)
	resp.Body.Close()
	require.Equal(t, originalBody, receivedBody)
}
