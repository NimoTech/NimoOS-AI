// route/v2/chat.go
package v2

import (
	"bufio"
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type ChatHandler struct {
	svc service.Services
}

func NewChatHandler(svc service.Services) *ChatHandler {
	return &ChatHandler{svc: svc}
}

// ChatCompletions handles POST /v1/ai/chat/completions (OpenAI format)
func (h *ChatHandler) ChatCompletions(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}

	forceCloud := c.Request().Header.Get("X-NimoOS-Force-Cloud") == "true"

	decision, err := h.svc.Router().Decide(userID, forceCloud)
	if err != nil {
		if errors.Is(err, service.ErrRemoteNotAllowed) {
			return echo.NewHTTPError(http.StatusForbidden, "remote access not allowed by privacy policy")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}

	body, err := io.ReadAll(c.Request().Body)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "failed to read request body")
	}

	body = stripProviderPrefix(body)
	stream := isStreamRequest(body)

	switch decision.Backend {
	case service.BackendLocal:
		return h.forwardToLocal(c, bytes.NewReader(body), stream)
	case service.BackendCloud:
		return h.forwardToCloud(c, userID, bytes.NewReader(body), stream)
	default:
		return echo.NewHTTPError(http.StatusInternalServerError, "unknown backend")
	}
}

func (h *ChatHandler) forwardToLocal(c echo.Context, body io.Reader, stream bool) error {
	resp, err := h.svc.LocalAdapter().ChatCompletions(body)
	if err != nil {
		return c.JSON(http.StatusServiceUnavailable, map[string]interface{}{
			"error": map[string]string{
				"code":    "local_model_failed",
				"type":    "escalation_required",
				"message": "Local model failed. Set X-NimoOS-Force-Cloud: true to retry with cloud.",
			},
		})
	}
	defer resp.Body.Close()
	return proxyResponse(c, resp)
}

func (h *ChatHandler) forwardToCloud(c echo.Context, userID string, body io.Reader, stream bool) error {
	providers, err := h.svc.Providers().ListProviders(userID)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, "failed to list providers")
	}
	if len(providers) == 0 {
		return echo.NewHTTPError(http.StatusBadRequest, "no cloud provider configured")
	}

	var provider *service.Provider
	for _, p := range providers {
		if p.Enabled {
			provider = p
			break
		}
	}
	if provider == nil {
		return echo.NewHTTPError(http.StatusBadRequest, "no enabled cloud provider")
	}

	apiKey, err := h.svc.MasterKey().Decrypt(provider.APIKey)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, "failed to decrypt api key")
	}

	switch provider.Protocol {
	case service.ProtocolOpenAI:
		adapter := service.NewOpenAIAdapter(provider.BaseURL, apiKey)
		resp, err := adapter.ChatCompletions(body)
		if err != nil {
			return echo.NewHTTPError(http.StatusBadGateway, err.Error())
		}
		defer resp.Body.Close()
		return proxyResponse(c, resp)

	case service.ProtocolAnthropic:
		adapter := service.NewAnthropicAdapter(provider.BaseURL, apiKey)
		resp, err := adapter.ChatCompletions(body)
		if err != nil {
			return echo.NewHTTPError(http.StatusBadGateway, err.Error())
		}
		defer resp.Body.Close()
		if !stream {
			return convertAnthropicResponseToOpenAI(c, resp)
		}
		return streamAnthropicToOpenAI(c, resp)
	}
	return echo.NewHTTPError(http.StatusBadRequest, "unknown protocol")
}

// Messages handles POST /v1/ai/messages (Anthropic native pass-through)
func (h *ChatHandler) Messages(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}

	// /messages always routes to Anthropic provider — does not participate in default_backend switching
	policy, err := h.svc.Providers().GetPolicy(userID)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return echo.NewHTTPError(http.StatusInternalServerError, "failed to load privacy policy")
	}
	if err == nil && !policy.AllowRemote {
		return echo.NewHTTPError(http.StatusForbidden, "remote access not allowed by privacy policy")
	}

	providers, err := h.svc.Providers().ListProviders(userID)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, "failed to list providers")
	}
	var provider *service.Provider
	for _, p := range providers {
		if p.Enabled && p.Protocol == service.ProtocolAnthropic {
			provider = p
			break
		}
	}
	if provider == nil {
		return echo.NewHTTPError(http.StatusBadRequest, "no Anthropic provider configured")
	}

	apiKey, err := h.svc.MasterKey().Decrypt(provider.APIKey)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, "failed to decrypt api key")
	}

	adapter := service.NewAnthropicAdapter(provider.BaseURL, apiKey)
	resp, err := adapter.Messages(c.Request().Body)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadGateway, err.Error())
	}
	defer resp.Body.Close()
	return proxyResponse(c, resp)
}

// stripProviderPrefix removes the "{providerID}:" prefix from the model field.
// The frontend encodes the provider ID into the model string (e.g. "6:deepseek-chat")
// so the backend can resolve the provider, but cloud APIs only accept the bare model name.
func stripProviderPrefix(body []byte) []byte {
	var req map[string]json.RawMessage
	if err := json.Unmarshal(body, &req); err != nil {
		return body
	}
	modelRaw, ok := req["model"]
	if !ok {
		return body
	}
	var model string
	if err := json.Unmarshal(modelRaw, &model); err != nil {
		return body
	}
	if idx := strings.Index(model, ":"); idx >= 0 {
		model = model[idx+1:]
		encoded, err := json.Marshal(model)
		if err != nil {
			return body
		}
		req["model"] = encoded
		out, err := json.Marshal(req)
		if err != nil {
			return body
		}
		return out
	}
	return body
}

// isStreamRequest parses the body bytes to check if stream:true was requested.
func isStreamRequest(body []byte) bool {
	var req struct {
		Stream bool `json:"stream"`
	}
	_ = json.Unmarshal(body, &req)
	return req.Stream
}

// proxyResponse copies upstream response headers and body to the client.
func proxyResponse(c echo.Context, resp *http.Response) error {
	for key, vals := range resp.Header {
		for _, v := range vals {
			c.Response().Header().Add(key, v)
		}
	}
	c.Response().WriteHeader(resp.StatusCode)
	_, err := io.Copy(c.Response(), resp.Body)
	return err
}

// convertAnthropicResponseToOpenAI converts a non-streaming Anthropic response to OpenAI format.
func convertAnthropicResponseToOpenAI(c echo.Context, resp *http.Response) error {
	var anthropicResp struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
		Model string `json:"model"`
		Usage struct {
			InputTokens  int `json:"input_tokens"`
			OutputTokens int `json:"output_tokens"`
		} `json:"usage"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&anthropicResp); err != nil {
		return echo.NewHTTPError(http.StatusBadGateway, "failed to decode Anthropic response")
	}
	text := ""
	if len(anthropicResp.Content) > 0 {
		text = anthropicResp.Content[0].Text
	}
	return c.JSON(http.StatusOK, map[string]interface{}{
		"object": "chat.completion",
		"model":  anthropicResp.Model,
		"choices": []map[string]interface{}{
			{"index": 0, "message": map[string]string{"role": "assistant", "content": text}, "finish_reason": "stop"},
		},
		"usage": map[string]int{
			"prompt_tokens":     anthropicResp.Usage.InputTokens,
			"completion_tokens": anthropicResp.Usage.OutputTokens,
		},
	})
}

// streamAnthropicToOpenAI converts Anthropic SSE stream to OpenAI SSE format.
func streamAnthropicToOpenAI(c echo.Context, resp *http.Response) error {
	c.Response().Header().Set("Content-Type", "text/event-stream")
	c.Response().Header().Set("Cache-Control", "no-cache")
	c.Response().Header().Set("Connection", "keep-alive")
	c.Response().WriteHeader(http.StatusOK)

	w := c.Response()
	flusher, ok := w.Writer.(http.Flusher)

	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		data := []byte(line[6:])
		if string(data) == "[DONE]" {
			fmt.Fprint(w, "data: [DONE]\n\n")
			if ok {
				flusher.Flush()
			}
			return nil
		}
		chunk, converted := service.ConvertAnthropicChunkToOpenAI(data, "")
		if !converted {
			continue
		}
		encoded, _ := json.Marshal(chunk)
		fmt.Fprintf(w, "data: %s\n\n", encoded)
		if ok {
			flusher.Flush()
		}
	}
	return scanner.Err()
}
