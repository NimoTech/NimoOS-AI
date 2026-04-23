// route/v2/chat_test.go
package v2

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

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
