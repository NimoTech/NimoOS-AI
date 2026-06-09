package v2

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
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

// The browser path injects X-NimoOS-User-ID (AI JWT middleware) before the proxy.
// The proxy must forward it to the Search service, or agent/tool returns 400.
func TestSearchProxy_ForwardsUserIDHeader(t *testing.T) {
	var mu sync.Mutex
	gotUID := ""
	gotName := ""
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotUID = r.Header.Get("X-NimoOS-User-ID")
		gotName = r.Header.Get("X-NimoOS-User-Name")
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"groups":{},"stats":{},"warnings":[]}`))
	}))
	defer upstream.Close()
	p := newSearchProxy(t, upstream.URL)

	e := echo.New()
	req := httptest.NewRequest("POST", "/v1/ai/search/agent/tool",
		strings.NewReader(`{"name":"nimoos_search","arguments":{"query":"x"}}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-NimoOS-User-ID", "42")
	req.Header.Set("X-NimoOS-User-Name", "alice")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("*")
	c.SetParamValues("agent/tool")

	if err := p.Proxy(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if gotUID != "42" {
		t.Fatalf("upstream X-NimoOS-User-ID = %q, want 42", gotUID)
	}
	if gotName != "alice" {
		t.Fatalf("upstream X-NimoOS-User-Name = %q, want alice", gotName)
	}
}
