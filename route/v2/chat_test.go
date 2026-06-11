// route/v2/chat_test.go
package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func TestChatHandler_MissingUserID_Returns401(t *testing.T) {
	e := echo.New()
	h := &ChatHandler{} // no services needed for auth check

	req := httptest.NewRequest(http.MethodPost, "/v1/ai/chat/completions",
		strings.NewReader(`{"model":"llama3","messages":[]}`))
	req.Header.Set(echo.HeaderContentType, "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	// user_id header NOT set

	err := h.ChatCompletions(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusUnauthorized, httpErr.Code)
}

func TestParseModelTarget(t *testing.T) {
	cases := []struct {
		name      string
		in        string
		wantBack  service.Backend
		wantPID   int64
		wantModel string
	}{
		{"local", `{"model":"local:llama3"}`, service.BackendLocal, 0, "llama3"},
		{"cloud_scheme", `{"model":"cloud:6:deepseek-chat"}`, service.BackendCloud, 6, "deepseek-chat"},
		{"legacy_numeric", `{"model":"6:deepseek-chat"}`, service.BackendCloud, 6, "deepseek-chat"},
		{"bare_no_prefix", `{"model":"gpt-4o"}`, service.Backend(""), 0, "gpt-4o"},
		{"cloud_no_id", `{"model":"cloud:deepseek-chat"}`, service.Backend(""), 0, "deepseek-chat"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			target, body := parseModelTarget([]byte(tc.in))
			require.Equal(t, tc.wantBack, target.backend)
			require.Equal(t, tc.wantPID, target.providerID)
			// body should carry the bare model name.
			var got struct {
				Model string `json:"model"`
			}
			require.NoError(t, json.Unmarshal(body, &got))
			require.Equal(t, tc.wantModel, got.Model)
		})
	}
}
