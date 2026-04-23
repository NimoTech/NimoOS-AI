package v2

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

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
