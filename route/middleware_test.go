package route

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"
)

func okHandler(c echo.Context) error {
	return c.String(http.StatusOK, "ok")
}

func TestLocalhostOnly_IPv4Allowed(t *testing.T) {
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.RemoteAddr = "127.0.0.1:54321"
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	h := LocalhostOnly(okHandler)
	if err := h(c); err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
}

func TestLocalhostOnly_IPv6Allowed(t *testing.T) {
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.RemoteAddr = "[::1]:54321"
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	h := LocalhostOnly(okHandler)
	if err := h(c); err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
}

func TestLocalhostOnly_RemoteForbidden(t *testing.T) {
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.RemoteAddr = "1.2.3.4:5000"
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	h := LocalhostOnly(okHandler)
	err := h(c)
	if err == nil {
		t.Fatal("expected error for remote addr, got nil")
	}
	he, ok := err.(*echo.HTTPError)
	if !ok {
		t.Fatalf("expected *echo.HTTPError, got %T", err)
	}
	if he.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", he.Code)
	}
}
