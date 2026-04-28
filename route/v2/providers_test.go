package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/NimoTech/NimoOS-AI/pkg/config"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

// newTestServices creates a real in-memory-ish Services backed by a temp SQLite DB
// and a throw-away master key — suitable for handler integration tests.
func newTestServices(t *testing.T) service.Services {
	t.Helper()
	dir := t.TempDir()
	cfg := &config.Config{
		DataPath:      dir,
		MasterKeyPath: dir + "/master.key",
	}
	return service.NewService(cfg)
}

func TestProvidersHandler_List_MissingUserID_Returns401(t *testing.T) {
	e := echo.New()
	h := &ProvidersHandler{}

	req := httptest.NewRequest(http.MethodGet, "/v1/ai/providers", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	err := h.List(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusUnauthorized, httpErr.Code)
}

func TestProvidersHandler_Update_InvalidID_Returns400(t *testing.T) {
	e := echo.New()
	h := &ProvidersHandler{}

	req := httptest.NewRequest(http.MethodPut, "/v1/ai/providers/abc", nil)
	req.Header.Set("X-NimoOS-User-ID", "user1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues("abc") // non-integer

	err := h.Update(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusBadRequest, httpErr.Code)
}

func TestProvidersHandler_Delete_MissingUserID_Returns401(t *testing.T) {
	e := echo.New()
	h := &ProvidersHandler{}

	req := httptest.NewRequest(http.MethodDelete, "/v1/ai/providers/1", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	err := h.Delete(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusUnauthorized, httpErr.Code)
}

func TestProvidersResponseIncludesThinkingFlags(t *testing.T) {
	svc := newTestServices(t)

	// Insert a DeepSeek provider (all DeepSeek models support thinking).
	err := svc.Providers().CreateProvider(&service.Provider{
		UserID:       "u1",
		Name:         "DS",
		BaseURL:      "https://api.deepseek.com/v1",
		APIKey:       "",
		Protocol:     service.ProtocolOpenAI,
		Enabled:      true,
		DefaultModel: "deepseek-v4-pro",
		ProviderType: "deepseek",
	})
	require.NoError(t, err)

	e := echo.New()
	h := NewProvidersHandler(svc)
	req := httptest.NewRequest(http.MethodGet, "/v1/ai/providers", nil)
	req.Header.Set("X-NimoOS-User-ID", "u1")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	require.NoError(t, h.List(c))

	var body []map[string]any
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &body))
	require.Len(t, body, 1)
	require.Equal(t, "deepseek", body[0]["provider_type"])
	require.Equal(t, true, body[0]["supports_thinking"])
}
