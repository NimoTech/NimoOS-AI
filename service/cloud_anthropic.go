package service

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
)

// ── OpenAI data structures ─────────────────────────────────────

// OpenAIMessage is a single message in the OpenAI messages array.
type OpenAIMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// OpenAIChatRequest is the OpenAI /v1/chat/completions request body.
type OpenAIChatRequest struct {
	Model     string          `json:"model"`
	Messages  []OpenAIMessage `json:"messages"`
	Stream    bool            `json:"stream"`
	MaxTokens int             `json:"max_tokens,omitempty"`
}

// OpenAIDelta is the delta object in an OpenAI streaming chunk.
type OpenAIDelta struct {
	Role    string `json:"role,omitempty"`
	Content string `json:"content,omitempty"`
}

// OpenAIChoice is a choice in an OpenAI response or streaming chunk.
type OpenAIChoice struct {
	Index        int         `json:"index"`
	Delta        OpenAIDelta `json:"delta"`
	FinishReason *string     `json:"finish_reason"`
}

// OpenAIStreamChunk is one SSE chunk in an OpenAI streaming response.
type OpenAIStreamChunk struct {
	ID      string         `json:"id,omitempty"`
	Object  string         `json:"object"`
	Model   string         `json:"model"`
	Choices []OpenAIChoice `json:"choices"`
}

// ── Anthropic data structures ──────────────────────────────────

// AnthropicMessage is a single message in the Anthropic messages array.
type AnthropicMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// AnthropicRequest is the Anthropic /v1/messages request body.
type AnthropicRequest struct {
	Model     string             `json:"model"`
	Messages  []AnthropicMessage `json:"messages"`
	System    string             `json:"system,omitempty"`
	MaxTokens int                `json:"max_tokens"`
	Stream    bool               `json:"stream"`
}

// anthropicChunkData is the data field of an Anthropic SSE event.
type anthropicChunkData struct {
	Type  string `json:"type"`
	Index int    `json:"index"`
	Delta struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"delta"`
}

// ── Protocol conversion ────────────────────────────────────────

// convertToAnthropic converts an OpenAI chat request to Anthropic format.
// It extracts the system message from the messages array into the top-level System field.
// If multiple system messages are present, only the last one is used.
func convertToAnthropic(req OpenAIChatRequest) AnthropicRequest {
	maxTokens := req.MaxTokens
	if maxTokens == 0 {
		maxTokens = 4096
	}

	var system string
	var messages []AnthropicMessage
	for _, m := range req.Messages {
		if m.Role == "system" {
			system = m.Content
		} else {
			messages = append(messages, AnthropicMessage{Role: m.Role, Content: m.Content})
		}
	}

	return AnthropicRequest{
		Model:     req.Model,
		Messages:  messages,
		System:    system,
		MaxTokens: maxTokens,
		Stream:    req.Stream,
	}
}

// ConvertAnthropicChunkToOpenAI converts an Anthropic SSE data payload to an OpenAI StreamChunk.
// Returns (chunk, true) if the event should be forwarded, (zero, false) if it should be skipped.
func ConvertAnthropicChunkToOpenAI(data []byte, model string) (OpenAIStreamChunk, bool) {
	var event anthropicChunkData
	if err := json.Unmarshal(data, &event); err != nil {
		return OpenAIStreamChunk{}, false
	}

	switch event.Type {
	case "content_block_delta":
		if event.Delta.Type != "text_delta" {
			return OpenAIStreamChunk{}, false
		}
		return OpenAIStreamChunk{
			Object: "chat.completion.chunk",
			Model:  model,
			Choices: []OpenAIChoice{{
				Index: event.Index,
				Delta: OpenAIDelta{Content: event.Delta.Text},
			}},
		}, true

	case "message_stop":
		reason := "stop"
		return OpenAIStreamChunk{
			Object: "chat.completion.chunk",
			Model:  model,
			Choices: []OpenAIChoice{{
				FinishReason: &reason,
			}},
		}, true
	}

	return OpenAIStreamChunk{}, false
}

// ── AnthropicAdapter ───────────────────────────────────────────

// AnthropicAdapter handles communication with the Anthropic API.
type AnthropicAdapter struct {
	baseURL string
	apiKey  string
	client  *http.Client
}

func NewAnthropicAdapter(baseURL, apiKey string) *AnthropicAdapter {
	return &AnthropicAdapter{
		baseURL: baseURL,
		apiKey:  apiKey,
		client:  &http.Client{}, // no timeout: streaming responses can be long
	}
}

func (a *AnthropicAdapter) setHeaders(req *http.Request) {
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-api-key", a.apiKey)
	req.Header.Set("anthropic-version", "2023-06-01")
}

// ChatCompletions accepts an OpenAI-format request, converts it to Anthropic format,
// and returns the raw upstream *http.Response. Caller owns resp.Body and is responsible
// for SSE parsing and protocol conversion for the /chat/completions endpoint.
func (a *AnthropicAdapter) ChatCompletions(body io.Reader) (*http.Response, error) {
	data, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}
	var openaiReq OpenAIChatRequest
	if err := json.Unmarshal(data, &openaiReq); err != nil {
		return nil, err
	}

	anthropicReq := convertToAnthropic(openaiReq)
	payload, err := json.Marshal(anthropicReq)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequest(http.MethodPost, a.baseURL+"/v1/messages", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	a.setHeaders(req)
	return a.client.Do(req)
}

// Messages transparently proxies an Anthropic-format request, only adding auth headers.
// Used by the /v1/ai/messages endpoint which accepts native Anthropic format.
func (a *AnthropicAdapter) Messages(body io.Reader) (*http.Response, error) {
	data, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(http.MethodPost, a.baseURL+"/v1/messages", bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	a.setHeaders(req)
	return a.client.Do(req)
}
