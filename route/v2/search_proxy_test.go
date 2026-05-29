package v2

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

func newSearchProxy(t *testing.T, upstreamURL string) *SearchProxy {
	t.Helper()
	dir := t.TempDir()
	discovery := filepath.Join(dir, "search.url")
	os.WriteFile(discovery, []byte(upstreamURL), 0644)
	return &SearchProxy{Client: service.NewSearchClient(discovery)}
}

// /v1/ai/search/text must rewrite to /v1/search/text and pass the body through.
func TestSearchProxy_TextRewritesPathAndForwardsBody(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/search/text": `{"hits":[{"score":0.9}],"warnings":[]}`,
	})
	defer srv.Close()
	p := newSearchProxy(t, srv.URL)

	e := echo.New()
	req := httptest.NewRequest("POST", "/v1/ai/search/text",
		strings.NewReader(`{"query":"甲状腺","top_k":10}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("*")
	c.SetParamValues("text")

	if err := p.Proxy(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"hits"`) {
		t.Fatalf("body = %s", rec.Body.String())
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "POST" ||
		(*captured)[0].Path != "/v1/search/text" ||
		!strings.Contains((*captured)[0].Body, `"query":"甲状腺"`) {
		t.Fatalf("captured = %+v", *captured)
	}
}

// Query string must survive the rewrite (e.g. GET /v1/ai/search/file?file_id=x).
func TestSearchProxy_PreservesQueryString(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/search/file": `{"chunks":[]}`,
	})
	defer srv.Close()
	p := newSearchProxy(t, srv.URL)

	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/search/file?file_id=abc&limit=50", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("*")
	c.SetParamValues("file")

	if err := p.Proxy(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Path != "/v1/search/file" {
		t.Fatalf("captured path = %+v", *captured)
	}
}
