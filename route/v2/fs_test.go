package v2

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"
)

func TestMountsReturnsAtLeastEmptyList(t *testing.T) {
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/fs/mounts", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	h := NewFSHandler()
	if err := h.Mounts(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if len(rec.Body.Bytes()) == 0 || rec.Body.String()[0] != '[' {
		t.Fatalf("expected JSON array, got %s", rec.Body.String())
	}
}
