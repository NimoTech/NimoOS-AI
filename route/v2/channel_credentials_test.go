package v2

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

const testInternalToken = "test-internal-token-value"

// credsCall writes a token file into a fresh temp dir, builds the handler
// bound to that dir, and issues the request with the matching
// X-Internal-Token header so existing 200/400/404/422 assertions exercise
// the code path after the internal-token guard.
func credsCall(t *testing.T, svc service.Services, userID, model string) *httptest.ResponseRecorder {
	t.Helper()
	dir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(dir, "ai_internal.token"),
		[]byte(testInternalToken), 0o600))
	h := ProviderCredentials(svc, dir)

	e := echo.New()
	req := httptest.NewRequest(http.MethodGet,
		"/_internal/agent/provider-credentials?user_id="+userID+"&model="+model, nil)
	req.Header.Set("X-Internal-Token", testInternalToken)
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

	rec := credsCall(t, svc, "u1", "cloud:"+strconv.FormatInt(p.ID, 10)+":deepseek-chat")
	require.Equal(t, http.StatusOK, rec.Code)
	body := rec.Body.String()
	require.Contains(t, body, `"api_key":"sk-secret"`)
	require.Contains(t, body, `"base_url":"https://api.deepseek.com"`)
	require.Contains(t, body, `"provider_type":"deepseek"`)
	require.Contains(t, body, `"model":"deepseek-chat"`)

	// wrong owner -> 404, never leaks the key
	rec = credsCall(t, svc, "u2", "cloud:"+strconv.FormatInt(p.ID, 10)+":deepseek-chat")
	require.Equal(t, http.StatusNotFound, rec.Code)
	require.NotContains(t, rec.Body.String(), "sk-secret")
}

func TestProviderCredentialsLocalAndErrors(t *testing.T) {
	svc := newTestServices(t)

	rec := credsCall(t, svc, "u1", "qwen3")
	require.Equal(t, http.StatusOK, rec.Code)
	require.Contains(t, rec.Body.String(), `"provider_type":"ollama"`)
	require.Contains(t, rec.Body.String(), `"base_url":"http://127.0.0.1:11434/v1"`)

	rec = credsCall(t, svc, "", "qwen3")
	require.Equal(t, http.StatusBadRequest, rec.Code)
	rec = credsCall(t, svc, "u1", "cloud:notanint:m")
	require.Equal(t, http.StatusBadRequest, rec.Code)
	rec = credsCall(t, svc, "u1", "cloud:999:m")
	require.Equal(t, http.StatusNotFound, rec.Code)
	rec = credsCall(t, svc, "u1", "openvino:whisper@GPU")
	require.Equal(t, http.StatusUnprocessableEntity, rec.Code)
}

// TestProviderCredentialsLocalPrefixStripped covers the bare-Ollama branch's
// handling of the UI's "local:<name>" model spec (listModelOptions /
// background_model): the resolver must strip the prefix before returning the
// model, mirroring chat.go:367 and the UI's agentStore.js — otherwise Ollama
// receives a literal "local:qwen3" and rejects it.
func TestProviderCredentialsLocalPrefixStripped(t *testing.T) {
	svc := newTestServices(t)

	cases := []struct {
		name       string
		inputModel string
		wantModel  string
	}{
		{"bare model name passes through unchanged", "qwen3", "qwen3"},
		{"local: prefix is stripped", "local:qwen3", "qwen3"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := credsCall(t, svc, "u1", tc.inputModel)
			require.Equal(t, http.StatusOK, rec.Code)
			require.Contains(t, rec.Body.String(), `"provider_type":"ollama"`)
			require.Contains(t, rec.Body.String(), `"model":"`+tc.wantModel+`"`)
		})
	}
}

func TestProviderCredentialsRejectsMissingOrWrongToken(t *testing.T) {
	svc := newTestServices(t)
	enc, err := svc.MasterKey().Encrypt("sk-should-not-leak")
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

	dir := t.TempDir()
	require.NoError(t, os.WriteFile(filepath.Join(dir, "ai_internal.token"),
		[]byte(testInternalToken), 0o600))
	h := ProviderCredentials(svc, dir)

	model := "cloud:" + strconv.FormatInt(p.ID, 10) + ":deepseek-chat"

	// (a) no X-Internal-Token header at all
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet,
		"/_internal/agent/provider-credentials?user_id=u1&model="+model, nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	require.NoError(t, h(c))
	require.Equal(t, http.StatusUnauthorized, rec.Code)
	require.NotContains(t, rec.Body.String(), "sk-should-not-leak")

	// (b) wrong token value
	req2 := httptest.NewRequest(http.MethodGet,
		"/_internal/agent/provider-credentials?user_id=u1&model="+model, nil)
	req2.Header.Set("X-Internal-Token", "wrong-token")
	rec2 := httptest.NewRecorder()
	c2 := e.NewContext(req2, rec2)
	require.NoError(t, h(c2))
	require.Equal(t, http.StatusUnauthorized, rec2.Code)
	require.NotContains(t, rec2.Body.String(), "sk-should-not-leak")
}
