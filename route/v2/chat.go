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
	"strconv"
	"strings"

	"github.com/NimoTech/NimoOS-AI/pkg/config"
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

	body, err := io.ReadAll(c.Request().Body)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "failed to read request body")
	}

	target, body := parseModelTarget(body)
	body = stripInternalFields(body)
	stream := isStreamRequest(body)

	// Explicit model selection drives routing; privacy policy only vetoes.
	switch target.backend {
	case service.BackendLocal:
		return h.forwardToLocal(c, bytes.NewReader(body), stream)
	case service.BackendOpenVINO:
		return h.forwardToOpenVINO(c, target, body, stream)
	case service.BackendCloud:
		// Reuse Decide(forceCloud=true) purely for the AllowRemote veto.
		if _, derr := h.svc.Router().Decide(userID, true); derr != nil {
			if errors.Is(derr, service.ErrRemoteNotAllowed) {
				return echo.NewHTTPError(http.StatusForbidden, "remote access not allowed by privacy policy")
			}
			return echo.NewHTTPError(http.StatusInternalServerError, derr.Error())
		}
		return h.forwardToCloud(c, userID, target.providerID, bytes.NewReader(body), stream)
	default:
		// No explicit prefix → legacy behaviour via Router.Decide.
		decision, derr := h.svc.Router().Decide(userID, forceCloud)
		if derr != nil {
			if errors.Is(derr, service.ErrRemoteNotAllowed) {
				return echo.NewHTTPError(http.StatusForbidden, "remote access not allowed by privacy policy")
			}
			return echo.NewHTTPError(http.StatusInternalServerError, derr.Error())
		}
		if decision.Backend == service.BackendLocal {
			return h.forwardToLocal(c, bytes.NewReader(body), stream)
		}
		return h.forwardToCloud(c, userID, 0, bytes.NewReader(body), stream)
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
	if stream {
		return proxySSEResponse(c, resp)
	}
	return proxyResponse(c, resp)
}

// forwardToOpenVINO routes a request to the local OVMS backend on the requested
// device. OpenVINO is a local backend, so it is not subject to the AllowRemote
// privacy policy (same as Ollama).
func (h *ChatHandler) forwardToOpenVINO(c echo.Context, target modelTarget, body []byte, stream bool) error {
	if config.Cfg != nil && !config.Cfg.OpenVINOEnabled {
		return c.JSON(http.StatusServiceUnavailable, map[string]interface{}{
			"error": map[string]interface{}{
				"code":    "backend_disabled",
				"type":    "service_unavailable",
				"message": "OpenVINO backend is disabled",
			},
		})
	}
	adapter := h.svc.OpenVINOAdapter()

	device := target.device
	if device == "" {
		device = adapter.DefaultDevice()
	}
	if !adapter.HasDevice(device) {
		return c.JSON(http.StatusBadRequest, map[string]interface{}{
			"error": map[string]interface{}{
				"code":      "unknown_device",
				"type":      "invalid_request_error",
				"message":   "device not resident on OpenVINO backend",
				"available": adapter.Devices(),
			},
		})
	}

	// On-demand load: ensure this model is loaded into OVMS (loads it, evicting
	// any other, on first use). Blocks until ready — first load can take minutes.
	if err := adapter.EnsureLoaded(target.bareModel, device); err != nil {
		return c.JSON(http.StatusServiceUnavailable, map[string]interface{}{
			"error": map[string]interface{}{
				"code":    "model_load_failed",
				"type":    "service_unavailable",
				"message": "OpenVINO model load failed: " + err.Error(),
			},
		})
	}

	// Rewrite the model field to the OVMS internal servable name.
	internal := service.OVMSModelName(target.bareModel, device)
	rewritten := setModelField(body, internal)

	resp, err := adapter.ChatCompletions(bytes.NewReader(rewritten))
	if err != nil {
		return c.JSON(http.StatusServiceUnavailable, map[string]interface{}{
			"error": map[string]interface{}{
				"code":    "backend_unavailable",
				"type":    "service_unavailable",
				"message": "OpenVINO service is not ready",
			},
		})
	}
	defer resp.Body.Close()
	if stream {
		return proxySSEResponse(c, resp)
	}
	return proxyResponse(c, resp)
}

func (h *ChatHandler) forwardToCloud(c echo.Context, userID string, providerID int64, body io.Reader, stream bool) error {
	var provider *service.Provider
	if providerID != 0 {
		p, err := h.svc.Providers().GetProvider(providerID, userID)
		if err != nil {
			return echo.NewHTTPError(http.StatusBadRequest, "selected provider not found")
		}
		if !p.Enabled {
			return echo.NewHTTPError(http.StatusBadRequest, "selected provider is disabled")
		}
		provider = p
	} else {
		providers, err := h.svc.Providers().ListProviders(userID)
		if err != nil {
			return echo.NewHTTPError(http.StatusInternalServerError, "failed to list providers")
		}
		if len(providers) == 0 {
			return echo.NewHTTPError(http.StatusBadRequest, "no cloud provider configured")
		}
		for _, p := range providers {
			if p.Enabled {
				provider = p
				break
			}
		}
		if provider == nil {
			return echo.NewHTTPError(http.StatusBadRequest, "no enabled cloud provider")
		}
	}

	apiKey, err := decryptProviderKey(h.svc.MasterKey(), provider.APIKey)
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
		bodyBytes, err := io.ReadAll(body)
		if err != nil {
			return echo.NewHTTPError(http.StatusBadRequest, "failed to read request body")
		}
		tc := extractThinkingControl(bodyBytes)
		adapter := service.NewAnthropicAdapter(provider.BaseURL, apiKey)
		resp, err := adapter.ChatCompletionsWithThinking(bytes.NewReader(bodyBytes), tc)
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

	apiKey, err := decryptProviderKey(h.svc.MasterKey(), provider.APIKey)
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

// extractThinkingControl reads the raw OpenAI-format request body and returns a
// ThinkingControl reflecting whatever thinking signal agent.py forwarded.
// It reads two optional fields:
//   - "reasoning_effort": "low"|"medium"|"high"|"max"  (maps 1:1 to thinking level)
//   - "extra_body": {"thinking": {"type": "enabled"|"disabled"}}
//
// extra_body.thinking.type takes precedence for the enabled/disabled toggle.
func extractThinkingControl(body []byte) service.ThinkingControl {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return service.ThinkingControl{}
	}

	tc := service.ThinkingControl{}

	if rawEffort, ok := raw["reasoning_effort"]; ok {
		var s string
		if err := json.Unmarshal(rawEffort, &s); err == nil {
			tc.Level = mapEffortToLevel(s)
			tc.Enabled = s != "minimal"
		}
	}

	if eb, ok := raw["extra_body"]; ok {
		var m map[string]json.RawMessage
		if err := json.Unmarshal(eb, &m); err == nil {
			if rawThinking, ok := m["thinking"]; ok {
				var thinking map[string]any
				if err := json.Unmarshal(rawThinking, &thinking); err == nil {
					if typ, _ := thinking["type"].(string); typ == "disabled" {
						tc.Enabled = false
					} else if typ == "enabled" {
						tc.Enabled = true
					}
				}
			}
		}
	}

	return tc
}

// mapEffortToLevel converts a reasoning_effort string to a ThinkingControl level.
func mapEffortToLevel(s string) string {
	switch s {
	case "low":
		return "low"
	case "medium":
		return "medium"
	case "high":
		return "high"
	case "max":
		return "max"
	}
	return "medium"
}

// modelTarget is the routing intent parsed from the request's model field.
// backend == "" means no explicit prefix → caller falls back to Router.Decide.
type modelTarget struct {
	backend    service.Backend
	providerID int64
	bareModel  string
	device     string // only used by the openvino backend; "" means use the default device
}

// parseModelTarget reads the model field, classifies the routing target, and
// returns the body rewritten with the bare model name. Recognised forms:
//
//	"local:<name>"             → local
//	"cloud:<id>:<name>"        → cloud, provider <id>
//	"<id>:<name>" (numeric id) → cloud, provider <id>  (legacy)
//	anything else              → backend "" (no explicit target)
func parseModelTarget(body []byte) (modelTarget, []byte) {
	var req map[string]json.RawMessage
	if err := json.Unmarshal(body, &req); err != nil {
		return modelTarget{}, body
	}
	modelRaw, ok := req["model"]
	if !ok {
		return modelTarget{}, body
	}
	var model string
	if err := json.Unmarshal(modelRaw, &model); err != nil {
		return modelTarget{}, body
	}

	tgt := modelTarget{bareModel: model}
	switch {
	case strings.HasPrefix(model, "openvino:"):
		tgt.backend = service.BackendOpenVINO
		rest := model[len("openvino:"):]
		if idx := strings.LastIndex(rest, "@"); idx >= 0 {
			tgt.bareModel = rest[:idx]
			tgt.device = rest[idx+1:]
		} else {
			tgt.bareModel = rest
		}
	case strings.HasPrefix(model, "local:"):
		tgt.backend = service.BackendLocal
		tgt.bareModel = model[len("local:"):]
	case strings.HasPrefix(model, "cloud:"):
		rest := model[len("cloud:"):]
		if idx := strings.Index(rest, ":"); idx > 0 {
			if id, perr := strconv.ParseInt(rest[:idx], 10, 64); perr == nil {
				tgt.backend = service.BackendCloud
				tgt.providerID = id
				tgt.bareModel = rest[idx+1:]
			} else {
				// "cloud:" prefix but unparseable id → strip prefix, route via default.
				tgt.bareModel = rest
			}
		} else {
			// "cloud:" prefix with no id segment → strip prefix, route via default.
			tgt.bareModel = rest
		}
	default:
		if idx := strings.Index(model, ":"); idx > 0 {
			if id, perr := strconv.ParseInt(model[:idx], 10, 64); perr == nil {
				tgt.backend = service.BackendCloud
				tgt.providerID = id
				tgt.bareModel = model[idx+1:]
			}
		}
	}

	// Rewrite body with the bare model name.
	if tgt.bareModel != model {
		if encoded, err := json.Marshal(tgt.bareModel); err == nil {
			req["model"] = encoded
			if out, err := json.Marshal(req); err == nil {
				return tgt, out
			}
		}
	}
	return tgt, body
}

// stripInternalFields removes NimoOS-internal fields (e.g. _backend) before forwarding
// to upstream APIs that don't understand them.
func stripInternalFields(body []byte) []byte {
	var req map[string]json.RawMessage
	if err := json.Unmarshal(body, &req); err != nil {
		return body
	}
	delete(req, "_backend")
	out, err := json.Marshal(req)
	if err != nil {
		return body
	}
	return out
}

// setModelField rewrites the "model" field of an OpenAI-format request body.
// Used to translate a user-facing model name into the OVMS internal servable name.
func setModelField(body []byte, model string) []byte {
	var req map[string]json.RawMessage
	if err := json.Unmarshal(body, &req); err != nil {
		return body
	}
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

// proxySSEResponse proxies a Server-Sent Events stream, flushing after each event
// so the client receives tokens as they are generated rather than in one batch.
func proxySSEResponse(c echo.Context, resp *http.Response) error {
	for key, vals := range resp.Header {
		for _, v := range vals {
			c.Response().Header().Add(key, v)
		}
	}
	c.Response().WriteHeader(resp.StatusCode)

	w := c.Response()
	flusher, canFlush := w.Writer.(http.Flusher)

	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Bytes()
		w.Write(line)
		w.Write([]byte("\n"))
		// Flush on blank line — that's the SSE event boundary.
		if len(line) == 0 && canFlush {
			flusher.Flush()
		}
	}
	return scanner.Err()
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
