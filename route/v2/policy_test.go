package v2

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func TestPolicyHandler_Get_MissingUserID_Returns401(t *testing.T) {
	e := echo.New()
	h := &PolicyHandler{}

	req := httptest.NewRequest(http.MethodGet, "/v1/ai/policy", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	err := h.Get(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusUnauthorized, httpErr.Code)
}

func TestPolicyHandler_Update_MissingUserID_Returns401(t *testing.T) {
	e := echo.New()
	h := &PolicyHandler{}

	req := httptest.NewRequest(http.MethodPut, "/v1/ai/policy", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	err := h.Update(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusUnauthorized, httpErr.Code)
}
