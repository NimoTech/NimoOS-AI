package v2

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/NimoTech/NimoOS-AI/pkg/config"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type AgentHandler struct {
	svc       service.Services
	agentURL  string
	timeout   int
	available atomic.Bool
	proxy     *httputil.ReverseProxy
	tickets   *TicketStore
}

func NewAgentHandler(svc service.Services, agentURL string, timeout int, tickets *TicketStore) *AgentHandler {
	target, _ := url.Parse(agentURL)
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.FlushInterval = 100 * time.Millisecond

	// Strip the /v1/ai prefix so /v1/ai/agent/sessions → /agent/sessions
	orig := proxy.Director
	proxy.Director = func(req *http.Request) {
		orig(req)
		req.URL.Path = strings.TrimPrefix(req.URL.Path, "/v1/ai")
		if req.URL.RawPath != "" {
			req.URL.RawPath = strings.TrimPrefix(req.URL.RawPath, "/v1/ai")
		}
	}

	h := &AgentHandler{
		svc:      svc,
		agentURL: agentURL,
		timeout:  timeout,
		proxy:    proxy,
		tickets:  tickets,
	}
	h.available.Store(false)
	return h
}

// StartHealthMonitor polls the Python service until available, then monitors every 30s.
func (h *AgentHandler) StartHealthMonitor() {
	go func() {
		deadline := time.Now().Add(30 * time.Second)
		for time.Now().Before(deadline) {
			if h.ping() {
				h.available.Store(true)
				break
			}
			time.Sleep(2 * time.Second)
		}
		for {
			time.Sleep(30 * time.Second)
			h.available.Store(h.ping())
		}
	}()
}

func (h *AgentHandler) ping() bool {
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(h.agentURL + "/agent/health")
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// Health exposes the Python service health state.
func (h *AgentHandler) Health(c echo.Context) error {
	if !h.available.Load() {
		return c.JSON(http.StatusServiceUnavailable, map[string]string{
			"status": "unavailable",
			"detail": "nimoos-agent service is not reachable",
		})
	}
	return c.JSON(http.StatusOK, map[string]string{"status": "ok"})
}

// Proxy forwards requests to the Python agent service.
func (h *AgentHandler) Proxy(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}

	if !h.available.Load() {
		return echo.NewHTTPError(http.StatusServiceUnavailable, "nimoos-agent is not available")
	}

	c.Request().Header.Set("X-User-Id", userID)

	// A fresh user with no skill_state rows never gets .runtime/<uid>/ built,
	// so the agent's list_skills tool would return []. Build it lazily here
	// so the very first agent request can see built-in skills.
	if h.svc != nil {
		h.svc.Skills().EnsureRuntimeView(userID)
	}

	if name := c.Request().Header.Get("X-NimoOS-User-Name"); name != "" {
		c.Request().Header.Set("X-User-Name", name)
	}

	providerType := c.Request().Header.Get("X-Agent-Provider-Type")
	// OpenVINO(OVMS):agent 路由不像 chat.go 有模型名前缀路由,这里检测 run body 里
	// 形如 "name@GPU.1" 的 OVMS 设备后缀模型,把 agent 指向 OVMS(OpenAI 兼容,无鉴权),
	// 并把 model 改写成 OVMS 内部 servable 名("name-gpu1")。优先级高于 provider_type。
	if h.routeOpenVINO(c) {
		c.Request().Header.Set("X-Agent-Provider-Key", "openvino")
		c.Request().Header.Set("X-Agent-Provider-Url", config.Cfg.OpenVINOURL+"/v3")
		// 让 Python 侧据此套用 Qwen 系的思考开关(think/enable_thinking),
		// UI 关闭思考时模型才会跳过冗长 reasoning、直接产出工具调用/答案。
		c.Request().Header.Set("X-Agent-Provider-Type", "openvino")
	} else if providerType == "ollama" {
		c.Request().Header.Set("X-Agent-Provider-Key", "ollama")
		c.Request().Header.Set("X-Agent-Provider-Url", "http://127.0.0.1:11434/v1")
	} else if h.svc != nil {
		// Prefer the explicitly selected provider; fall back to first-enabled.
		var key, provURL string
		var ok bool
		if pid := c.Request().Header.Get("X-Agent-Provider-Id"); pid != "" {
			if id, perr := strconv.ParseInt(pid, 10, 64); perr == nil {
				key, provURL, ok = h.resolveProviderByID(userID, id)
			}
		}
		if !ok {
			key, provURL, ok = h.resolveProvider(userID)
		}
		if ok {
			c.Request().Header.Set("X-Agent-Provider-Key", key)
			c.Request().Header.Set("X-Agent-Provider-Url", provURL)
		}
	}

	// Inject base64(JSON([patterns])) for the user's hard blacklist.
	if h.svc != nil {
		patterns, err := h.svc.Blacklist().ListPatterns(userID)
		if err == nil && len(patterns) > 0 {
			if buf, err := json.Marshal(patterns); err == nil {
				enc := base64.StdEncoding.EncodeToString(buf)
				c.Request().Header.Set("X-Agent-User-Blacklist", enc)
			}
		}
	}

	// MCP: mint a one-time ticket so the agent can pull this user's decrypted
	// MCP config from the loopback /_internal/mcp/runtime endpoint without a JWT.
	if h.tickets != nil && isRunEndpoint(c.Request()) {
		c.Request().Header.Set("X-Agent-MCP-Ticket", h.tickets.Mint(userID))
	}

	if isRunEndpoint(c.Request()) {
		rc := http.NewResponseController(c.Response().Writer)
		_ = rc.SetWriteDeadline(time.Time{})
	}

	h.proxy.ServeHTTP(c.Response().Writer, c.Request())
	return nil
}

// routeOpenVINO inspects a JSON request body for an OVMS device-suffixed model
// name ("name@GPU.1") and, when found, rewrites the body's "model" field to the
// OVMS internal servable name ("name-gpu1") so the agent's OpenAI client talks
// to OVMS directly. Returns true iff the request was rewritten for OpenVINO.
// The body is always restored (rewritten or not) so the reverse proxy can
// forward it. Non-JSON / bodyless requests are ignored.
func (h *AgentHandler) routeOpenVINO(c echo.Context) bool {
	req := c.Request()
	if req.Body == nil || !strings.HasPrefix(req.Header.Get("Content-Type"), "application/json") {
		return false
	}
	body, err := io.ReadAll(req.Body)
	req.Body.Close()
	restore := func(b []byte) {
		req.Body = io.NopCloser(bytes.NewReader(b))
		req.ContentLength = int64(len(b))
		req.Header.Set("Content-Length", strconv.Itoa(len(b)))
	}
	if err != nil {
		restore(body)
		return false
	}
	var m map[string]json.RawMessage
	var model string
	if json.Unmarshal(body, &m) != nil {
		restore(body)
		return false
	}
	if raw, ok := m["model"]; !ok || json.Unmarshal(raw, &model) != nil {
		restore(body)
		return false
	}
	bare, device, ok := parseOVMSDeviceSuffix(model)
	if !ok {
		restore(body)
		return false
	}
	enc, err := json.Marshal(service.OVMSModelName(bare, device))
	if err != nil {
		restore(body)
		return false
	}
	m["model"] = enc
	nb, err := json.Marshal(m)
	if err != nil {
		restore(body)
		return false
	}
	restore(nb)
	return true
}

// parseOVMSDeviceSuffix splits "name@GPU.1" into ("name","GPU.1",true). It only
// matches a trailing @<device> where device starts with GPU/NPU or equals CPU —
// the device forms OVMSModelName produces. Anything else returns ok=false.
func parseOVMSDeviceSuffix(model string) (bare, device string, ok bool) {
	i := strings.LastIndex(model, "@")
	if i <= 0 || i == len(model)-1 {
		return "", "", false
	}
	bare, device = model[:i], model[i+1:]
	up := strings.ToUpper(device)
	if strings.HasPrefix(up, "GPU") || strings.HasPrefix(up, "NPU") || up == "CPU" {
		return bare, device, true
	}
	return "", "", false
}

func (h *AgentHandler) resolveProvider(userID string) (key, provURL string, ok bool) {
	providers, err := h.svc.Providers().ListProviders(userID)
	if err != nil || len(providers) == 0 {
		return "", "", false
	}
	for _, p := range providers {
		if p.Enabled {
			decrypted, err := h.svc.MasterKey().Decrypt(p.APIKey)
			if err != nil {
				continue
			}
			return decrypted, p.BaseURL, true
		}
	}
	return "", "", false
}

// resolveProviderByID resolves a specific provider's decrypted key + base URL.
// Returns ok=false if missing, not owned by userID, disabled, or undecryptable.
func (h *AgentHandler) resolveProviderByID(userID string, id int64) (key, provURL string, ok bool) {
	p, err := h.svc.Providers().GetProvider(id, userID)
	if err != nil || !p.Enabled {
		return "", "", false
	}
	decrypted, err := h.svc.MasterKey().Decrypt(p.APIKey)
	if err != nil {
		return "", "", false
	}
	return decrypted, p.BaseURL, true
}

func isRunEndpoint(r *http.Request) bool {
	return r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/run")
}
