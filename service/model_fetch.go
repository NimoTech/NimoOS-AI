package service

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// fetchTimeout caps every provider /models call. CPU-only NAS boxes have hung
// on slow upstreams before — never wait unbounded.
const fetchTimeout = 8 * time.Second

// FetchModels queries a provider's model-listing endpoint and returns model
// names. apiKey is the DECRYPTED key (caller decrypts). Errors are non-fatal to
// the system: the handler surfaces them as a warning and keeps stored models.
func FetchModels(p *Provider, apiKey string) ([]string, error) {
	client := &http.Client{Timeout: fetchTimeout}

	// Ollama exposes a native tag list at /api/tags (not under /v1).
	if p.ProviderType == "ollama" {
		base := strings.TrimSuffix(strings.TrimRight(p.BaseURL, "/"), "/v1")
		return fetchOllamaTags(client, base)
	}

	url := strings.TrimRight(p.BaseURL, "/") + "/models"
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	switch p.Protocol {
	case ProtocolAnthropic:
		req.Header.Set("x-api-key", apiKey)
		req.Header.Set("anthropic-version", "2023-06-01")
	default: // openai-compatible
		req.Header.Set("Authorization", "Bearer "+apiKey)
	}

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("provider /models returned status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var parsed struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, err
	}
	out := make([]string, 0, len(parsed.Data))
	for _, m := range parsed.Data {
		if m.ID != "" {
			out = append(out, m.ID)
		}
	}
	return out, nil
}

func fetchOllamaTags(client *http.Client, base string) ([]string, error) {
	resp, err := client.Get(base + "/api/tags")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("ollama /api/tags returned status %d", resp.StatusCode)
	}
	var parsed struct {
		Models []struct {
			Name string `json:"name"`
		} `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, err
	}
	out := make([]string, 0, len(parsed.Models))
	for _, m := range parsed.Models {
		if m.Name != "" {
			out = append(out, m.Name)
		}
	}
	return out, nil
}
