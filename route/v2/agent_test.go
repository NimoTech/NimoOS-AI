package v2_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	v2 "github.com/NimoTech/NimoOS-AI/route/v2"
	"github.com/labstack/echo/v4"
)

func TestAgentHealth_PythonDown(t *testing.T) {
	e := echo.New()
	h := v2.NewAgentHandler(nil, "http://127.0.0.1:19999", 10, nil) // unreachable port
	req := httptest.NewRequest(http.MethodGet, "/agent/health", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	_ = h.Health(c)
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("expected 503, got %d", rec.Code)
	}
}

func TestAgentProxy_MissingUserID(t *testing.T) {
	e := echo.New()
	h := v2.NewAgentHandler(nil, "http://127.0.0.1:19999", 10, nil)
	req := httptest.NewRequest(http.MethodPost, "/agent/sessions", strings.NewReader("{}"))
	req.Header.Set("Content-Type", "application/json")
	// No X-NimoOS-User-ID header set
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	err := h.Proxy(c)
	if err == nil && rec.Code != http.StatusUnauthorized {
		t.Errorf("expected 401, got %d", rec.Code)
	}
}
