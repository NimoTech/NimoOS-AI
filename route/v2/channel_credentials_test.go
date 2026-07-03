package v2

import (
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func credsCall(t *testing.T, h echo.HandlerFunc, userID, model string) *httptest.ResponseRecorder {
	t.Helper()
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet,
		"/_internal/agent/provider-credentials?user_id="+userID+"&model="+model, nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	require.NoError(t, h(c))
	return rec
}

func TestProviderCredentialsCloud(t *testing.T) {
	svc := newTestServices(t)
	enc, err := svc.MasterKey().Encrypt("sk-secret")
	require.NoError(t, err)
	p := &service.Provider{
		UserID:       "u1",
		Name:         "deepseek",
		BaseURL:      "https://api.deepseek.com",
		APIKey:       enc,
		Protocol:     service.ProtocolOpenAI,
		Enabled:      true,
		DefaultModel: "deepseek-chat",
		ProviderType: "deepseek",
	}
	require.NoError(t, svc.Providers().CreateProvider(p))

	h := ProviderCredentials(svc)
	rec := credsCall(t, h, "u1", "cloud:"+strconv.FormatInt(p.ID, 10)+":deepseek-chat")
	require.Equal(t, http.StatusOK, rec.Code)
	body := rec.Body.String()
	require.Contains(t, body, `"api_key":"sk-secret"`)
	require.Contains(t, body, `"base_url":"https://api.deepseek.com"`)
	require.Contains(t, body, `"provider_type":"deepseek"`)
	require.Contains(t, body, `"model":"deepseek-chat"`)

	// wrong owner -> 404, never leaks the key
	rec = credsCall(t, h, "u2", "cloud:"+strconv.FormatInt(p.ID, 10)+":deepseek-chat")
	require.Equal(t, http.StatusNotFound, rec.Code)
	require.NotContains(t, rec.Body.String(), "sk-secret")
}

func TestProviderCredentialsLocalAndErrors(t *testing.T) {
	svc := newTestServices(t)
	h := ProviderCredentials(svc)

	rec := credsCall(t, h, "u1", "qwen3")
	require.Equal(t, http.StatusOK, rec.Code)
	require.Contains(t, rec.Body.String(), `"provider_type":"ollama"`)
	require.Contains(t, rec.Body.String(), `"base_url":"http://127.0.0.1:11434/v1"`)

	rec = credsCall(t, h, "", "qwen3")
	require.Equal(t, http.StatusBadRequest, rec.Code)
	rec = credsCall(t, h, "u1", "cloud:notanint:m")
	require.Equal(t, http.StatusBadRequest, rec.Code)
	rec = credsCall(t, h, "u1", "cloud:999:m")
	require.Equal(t, http.StatusNotFound, rec.Code)
	rec = credsCall(t, h, "u1", "openvino:whisper@GPU")
	require.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}
