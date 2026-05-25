package v2

import (
	"io"
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

type capturedReq struct {
	Method string
	Path   string
	Body   string
}

func startUpstream(t *testing.T, respByPath map[string]string) (*httptest.Server, *[]capturedReq, *sync.Mutex) {
	t.Helper()
	captured := []capturedReq{}
	mu := &sync.Mutex{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		captured = append(captured, capturedReq{r.Method, r.URL.Path, string(b)})
		mu.Unlock()
		if resp, ok := respByPath[r.URL.Path]; ok {
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(resp))
			return
		}
		http.NotFound(w, r)
	}))
	return srv, &captured, mu
}

func newProxy(t *testing.T, upstreamURL string) *ParserProxy {
	dir := t.TempDir()
	discovery := filepath.Join(dir, "parser.url")
	os.WriteFile(discovery, []byte(upstreamURL), 0644)
	return &ParserProxy{Client: service.NewParserClient(discovery)}
}

func TestParserProxy_StatsPassesThrough(t *testing.T) {
	srv, _, _ := startUpstream(t, map[string]string{
		"/v1/parser/stats": `{"queue_depth":{"pending":3,"running":1,"failed":0,"done":2}}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/parser/stats", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.Stats(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"pending":3`) {
		t.Fatalf("body = %s", rec.Body.String())
	}
}

func TestParserProxy_ControlValidatesUnknownAction(t *testing.T) {
	srv, _, _ := startUpstream(t, nil)
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("POST", "/v1/ai/parser/control",
		strings.NewReader(`{"action":"unknown"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.Control(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 400 {
		t.Fatalf("code = %d, want 400", rec.Code)
	}
}

func TestParserProxy_ControlPauseForwards(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/parser/control/pause": `{"paused":true}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("POST", "/v1/ai/parser/control",
		strings.NewReader(`{"action":"pause"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.Control(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "POST" ||
		(*captured)[0].Path != "/v1/parser/control/pause" {
		t.Fatalf("captured = %+v", *captured)
	}
}

func TestParserProxy_ControlSetConcurrencyForwards(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/parser/control/concurrency": `{"concurrency":1}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("POST", "/v1/ai/parser/control",
		strings.NewReader(`{"action":"set_concurrency","n":1}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.Control(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Path != "/v1/parser/control/concurrency" ||
		!strings.Contains((*captured)[0].Body, `"n":1`) {
		t.Fatalf("captured = %+v", *captured)
	}
}
