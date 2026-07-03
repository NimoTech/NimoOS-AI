package v2

import (
	"net/http"
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

// ProviderCredentials serves the localhost-only internal endpoint that lets
// the in-process Python channel workers obtain decrypted provider
// credentials for a user. Registered under /_internal (LocalhostOnly,
// JWT-skipped, never registered with the Gateway).
func ProviderCredentials(svc service.Services) echo.HandlerFunc {
	return func(c echo.Context) error {
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
			// AgentHandler.Proxy (route/v2/agent.go).
			return c.JSON(http.StatusOK, providerCredentials{
				ProviderType: "ollama",
				BaseURL:      "http://127.0.0.1:11434/v1",
				APIKey:       "ollama",
				Model:        model,
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
