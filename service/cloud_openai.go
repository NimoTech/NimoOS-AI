package service

import (
	"bytes"
	"io"
	"net/http"
)

// OpenAIAdapter proxies requests to any OpenAI-compatible API endpoint.
type OpenAIAdapter struct {
	baseURL string
	apiKey  string
	client  *http.Client
}

func NewOpenAIAdapter(baseURL, apiKey string) *OpenAIAdapter {
	return &OpenAIAdapter{
		baseURL: baseURL,
		apiKey:  apiKey,
		client:  &http.Client{}, // no timeout: streaming responses
	}
}

// ChatCompletions forwards the request to baseURL/chat/completions with Bearer auth.
// baseURL is expected to already include the version prefix (e.g. https://api.openai.com/v1).
// The request body is passed unchanged. Returns raw *http.Response; caller owns resp.Body.
func (a *OpenAIAdapter) ChatCompletions(body io.Reader) (*http.Response, error) {
	data, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(http.MethodPost, a.baseURL+"/chat/completions", bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+a.apiKey)
	return a.client.Do(req)
}
