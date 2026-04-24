package v2

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"
	"github.com/stretchr/testify/require"
)

func TestModelsHandler_List_MissingUserID_Returns401(t *testing.T) {
	e := echo.New()
	h := &ModelsHandler{}

	req := httptest.NewRequest(http.MethodGet, "/v1/ai/models", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	err := h.List(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusUnauthorized, httpErr.Code)
}

func TestModelsHandler_Delete_MissingUserID_Returns401(t *testing.T) {
	e := echo.New()
	h := &ModelsHandler{}

	req := httptest.NewRequest(http.MethodDelete, "/v1/ai/models/llama3:8b", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	err := h.Delete(c)
	var httpErr *echo.HTTPError
	require.ErrorAs(t, err, &httpErr)
	require.Equal(t, http.StatusUnauthorized, httpErr.Code)
}
