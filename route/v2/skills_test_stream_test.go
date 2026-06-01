package v2

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/labstack/echo/v4"
)

func TestSkillsTestStream_RequiresAuth(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	body, _ := json.Marshal(map[string]any{"prompt": "hi"})
	req := httptest.NewRequest(http.MethodPost, "/skills/hello/test", bytes.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues("hello")
	err := h.TestStream(c)
	if err == nil {
		t.Fatal("expected 401")
	}
}
