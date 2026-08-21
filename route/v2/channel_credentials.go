package v2

import (
	"crypto/subtle"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/labstack/echo/v4"

	"github.com/NimoTech/NimoOS-AI/service"
)

type providerCredentials struct {
	ProviderType string `json:"provider_type"`
	BaseURL      string `json:"base_url"`
	APIKey       string `json:"api_key"`
	Model        string `json:"model"`
}

// ValidInternalToken reports whether the request carries the positive
// shared-secret X-Internal-Token matching {runtimePath}/ai_internal.token.
//
// LocalhostOnly alone is insufficient for internal endpoints that accept a
// caller-supplied user_id: the Gateway reverse-proxies /v1/ai/* (incl.
// /_internal/*) from loopback, satisfying LocalhostOnly, and — for the
// agent's own sandboxed processes — network_mode: host plus an
// egress-proxy that skips policy for internal targets means any process
// reachable from inside the container can also hit loopback. The token
// check is the actual boundary; mirrors the X-Agent-MCP-Ticket guard on
// /_internal/mcp/runtime.
func ValidInternalToken(runtimePath string, r *http.Request) bool {
	expected, rerr := os.ReadFile(filepath.Join(runtimePath, "ai_internal.token"))
	if rerr != nil || len(expected) == 0 {
		return false
	}
	return subtle.ConstantTimeCompare(expected,
		[]byte(r.Header.Get("X-Internal-Token"))) == 1
}

// InternalTokenOnly gates a route on ValidInternalToken, mirroring AdminOnly's
// shape so it can be attached the same way route/v2.go attaches AdminOnly.
func InternalTokenOnly(runtimePath string) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			if !ValidInternalToken(runtimePath, c.Request()) {
				return c.JSON(http.StatusUnauthorized,
					map[string]string{"message": "unauthorized"})
			}
			return next(c)
		}
	}
}

// ProviderCredentials serves the localhost-only internal endpoint that lets
// the in-process Python channel workers obtain decrypted provider
// credentials for a user. Registered under /_internal (LocalhostOnly,
// JWT-skipped, never registered with the Gateway).
func ProviderCredentials(svc service.Services, runtimePath string) echo.HandlerFunc {
	return func(c echo.Context) error {
		if !ValidInternalToken(runtimePath, c.Request()) {
			return c.JSON(http.StatusUnauthorized,
				map[string]string{"message": "unauthorized"})
		}

		userID := c.QueryParam("user_id")
		model := c.QueryParam("model")
		if userID == "" || model == "" {
			return c.JSON(http.StatusBadRequest,
				map[string]string{"message": "user_id and model required"})
		}
		if strings.HasPrefix(model, "openvino:") {
			return c.JSON(http.StatusUnprocessableEntity,
				map[string]string{"message": "openvino models not supported on channels"})
		}
		if !strings.HasPrefix(model, "cloud:") {
			// Bare model name = local Ollama. Mirrors the hardcoded branch in
			// AgentHandler.Proxy (route/v2/agent.go). Model may still carry the
			// UI's "local:" prefix (listModelOptions/background_model); strip
			// it so bare Ollama sees a real model name, matching chat.go:367
			// and UI agentStore.js's own prefix stripping.
			return c.JSON(http.StatusOK, providerCredentials{
				ProviderType: "ollama",
				BaseURL:      "http://127.0.0.1:11434/v1",
				APIKey:       "ollama",
				Model:        strings.TrimPrefix(model, "local:"),
			})
		}
		parts := strings.SplitN(model, ":", 3)
		if len(parts) != 3 || parts[1] == "" || parts[2] == "" {
			return c.JSON(http.StatusBadRequest,
				map[string]string{"message": "bad cloud model spec"})
		}
		id, err := strconv.ParseInt(parts[1], 10, 64)
		if err != nil {
			return c.JSON(http.StatusBadRequest,
				map[string]string{"message": "bad provider id"})
		}
		p, err := svc.Providers().GetProvider(id, userID)
		if err != nil || p == nil || !p.Enabled {
			return c.JSON(http.StatusNotFound,
				map[string]string{"message": "provider not found"})
		}
		key, err := svc.MasterKey().Decrypt(p.APIKey)
		if err != nil {
			return c.JSON(http.StatusInternalServerError,
				map[string]string{"message": "decrypt failed"})
		}
		return c.JSON(http.StatusOK, providerCredentials{
			ProviderType: p.ProviderType,
			BaseURL:      p.BaseURL,
			APIKey:       key,
			Model:        parts[2],
		})
	}
}
