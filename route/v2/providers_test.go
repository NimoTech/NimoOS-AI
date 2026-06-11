package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
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

func TestProvidersHandler_RefreshAndListModels(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"data":[{"id":"gpt-4o"},{"id":"o3"}]}`))
	}))
	defer upstream.Close()

	svc := newTestServices(t)
	p := &service.Provider{UserID: "10", Name: "OAI", BaseURL: upstream.URL, Protocol: service.ProtocolOpenAI, Enabled: true}
	require.NoError(t, svc.Providers().CreateProvider(p))

	h := NewProvidersHandler(svc)
	e := echo.New()

	// Refresh.
	req := httptest.NewRequest(http.MethodPost, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "10")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues(strconv.FormatInt(p.ID, 10))
	require.NoError(t, h.RefreshModels(c))
	require.Equal(t, http.StatusOK, rec.Code)

	// ListModels returns the fetched models.
	req2 := httptest.NewRequest(http.MethodGet, "/", nil)
	req2.Header.Set("X-NimoOS-User-ID", "10")
	rec2 := httptest.NewRecorder()
	c2 := e.NewContext(req2, rec2)
	c2.SetParamNames("id")
	c2.SetParamValues(strconv.FormatInt(p.ID, 10))
	require.NoError(t, h.ListModels(c2))
	var list []providerModelDTO
	require.NoError(t, json.Unmarshal(rec2.Body.Bytes(), &list))
	require.Len(t, list, 2)
}

func TestProvidersHandler_UpdateModels_FavoritesAndList(t *testing.T) {
	svc := newTestServices(t)
	p := &service.Provider{UserID: "10", Name: "DS", BaseURL: "https://api.deepseek.com/v1", Protocol: service.ProtocolOpenAI, Enabled: true}
	require.NoError(t, svc.Providers().CreateProvider(p))
	require.NoError(t, svc.Providers().UpsertFetchedModels(p.ID, []string{"deepseek-chat", "deepseek-reasoner"}))

	h := NewProvidersHandler(svc)
	e := echo.New()
	body := `{"models":[{"name":"deepseek-chat","favorite":true}]}`
	req := httptest.NewRequest(http.MethodPut, "/", strings.NewReader(body))
	req.Header.Set("X-NimoOS-User-ID", "10")
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues(strconv.FormatInt(p.ID, 10))
	require.NoError(t, h.UpdateModels(c))
	require.Equal(t, http.StatusOK, rec.Code)

	favs, err := svc.Providers().ListFavoriteModels(p.ID)
	require.NoError(t, err)
	require.Len(t, favs, 1)
	require.Equal(t, "deepseek-chat", favs[0].ModelName)
}

func TestProvidersHandler_List_EmbedsFavoritesOnly(t *testing.T) {
	svc := newTestServices(t)
	p := &service.Provider{UserID: "10", Name: "DS", BaseURL: "https://api.deepseek.com/v1", Protocol: service.ProtocolOpenAI, Enabled: true}
	require.NoError(t, svc.Providers().CreateProvider(p))
	require.NoError(t, svc.Providers().UpsertFetchedModels(p.ID, []string{"a", "b"}))
	_, err := svc.Providers().ReconcileModels(p.ID, []service.ProviderModelInput{{Name: "a", Favorite: true}})
	require.NoError(t, err)

	h := NewProvidersHandler(svc)
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-NimoOS-User-ID", "10")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	require.NoError(t, h.List(c))

	var list []providerDTO
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &list))
	require.Len(t, list, 1)
	require.Len(t, list[0].Models, 1, "only favorite models embedded")
	require.Equal(t, "a", list[0].Models[0].Name)
}
