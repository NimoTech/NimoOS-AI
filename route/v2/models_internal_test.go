package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func TestListInternal_MissingUserID_Returns400(t *testing.T) {
	e := echo.New()
	h := &ModelsHandler{}

	req := httptest.NewRequest(http.MethodGet, "/v1/ai/_internal/models", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	err := h.ListInternal(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusBadRequest, httpErr.Code)
}

func TestListInternal_ReturnsLocalAndCloud(t *testing.T) {
	svc := newTestServices(t)

	// Insert an enabled provider for user "7"
	err := svc.Providers().CreateProvider(&service.Provider{
		UserID:       "7",
		Name:         "OpenAI",
		BaseURL:      "https://api.openai.com/v1",
		APIKey:       "",
		Protocol:     service.ProtocolOpenAI,
		Enabled:      true,
		DefaultModel: "gpt-4o",
		ProviderType: "openai",
	})
	require.NoError(t, err)

	// Insert a disabled provider — must NOT appear in cloud list
	err = svc.Providers().CreateProvider(&service.Provider{
		UserID:       "7",
		Name:         "DisabledCloud",
		BaseURL:      "https://example.com/v1",
		APIKey:       "",
		Protocol:     service.ProtocolOpenAI,
		Enabled:      false,
		DefaultModel: "old-model",
		ProviderType: "other",
	})
	require.NoError(t, err)

	e := echo.New()
	// Ollama is not running in tests → ModelManager.ListModels() will fail;
	// handler must return empty local list (not an error).
	h := NewModelsHandler(svc, t.TempDir())

	req := httptest.NewRequest(http.MethodGet, "/v1/ai/_internal/models?user_id=7", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	require.NoError(t, h.ListInternal(c))
	require.Equal(t, http.StatusOK, rec.Code)

	var body map[string]any
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body))

	// local key must be present (empty list when Ollama not running)
	localList, ok := body["local"].([]any)
	require.True(t, ok, "local must be a JSON array")
	_ = localList // may be empty; that's fine

	// cloud key must contain exactly the enabled provider
	cloudList, ok := body["cloud"].([]any)
	require.True(t, ok, "cloud must be a JSON array")
	require.Len(t, cloudList, 1, "only the enabled provider must appear")

	p := cloudList[0].(map[string]any)
	require.Equal(t, "OpenAI", p["provider_name"])
	require.Equal(t, "gpt-4o", p["default_model"])
	require.Equal(t, true, p["enabled"])
}
