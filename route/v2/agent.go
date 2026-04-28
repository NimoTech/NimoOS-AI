package v2

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync/atomic"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type AgentHandler struct {
	svc       service.Services
	agentURL  string
	timeout   int
	available atomic.Bool
	proxy     *httputil.ReverseProxy
}

func NewAgentHandler(svc service.Services, agentURL string, timeout int) *AgentHandler {
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

	if name := c.Request().Header.Get("X-NimoOS-User-Name"); name != "" {
		c.Request().Header.Set("X-User-Name", name)
	}

	providerType := c.Request().Header.Get("X-Agent-Provider-Type")
	if providerType == "ollama" {
		c.Request().Header.Set("X-Agent-Provider-Key", "ollama")
		c.Request().Header.Set("X-Agent-Provider-Url", "http://127.0.0.1:11434/v1")
	} else if h.svc != nil {
		if key, provURL, ok := h.resolveProvider(userID); ok {
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

	if isRunEndpoint(c.Request()) {
		rc := http.NewResponseController(c.Response().Writer)
		_ = rc.SetWriteDeadline(time.Time{})
	}

	h.proxy.ServeHTTP(c.Response().Writer, c.Request())
	return nil
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

func isRunEndpoint(r *http.Request) bool {
	return r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/run")
}
