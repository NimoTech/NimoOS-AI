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

func TestParserProxy_DeleteJob(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/parser/jobs/42": ``,
	})
	defer srv.Close()
	// Override the handler to return 204 for DELETE
	srv2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		*captured = append(*captured, capturedReq{r.Method, r.URL.Path, string(b)})
		mu.Unlock()
		w.WriteHeader(204)
	}))
	defer srv2.Close()
	p := newProxy(t, srv2.URL)
	e := echo.New()
	req := httptest.NewRequest("DELETE", "/v1/ai/parser/jobs/42", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues("42")
	if err := p.DeleteJob(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 204 {
		t.Fatalf("code = %d, want 204", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "DELETE" || (*captured)[0].Path != "/v1/parser/jobs/42" {
		t.Fatalf("captured = %+v", *captured)
	}
}

func TestParserProxy_ClearFailedJobs(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/parser/jobs/clear-failed": `{"cleared":3}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("POST", "/v1/ai/parser/jobs/clear-failed", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.ClearFailedJobs(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"cleared":3`) {
		t.Fatalf("body = %s", rec.Body.String())
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "POST" || (*captured)[0].Path != "/v1/parser/jobs/clear-failed" {
		t.Fatalf("captured = %+v", *captured)
	}
}

func TestParserProxy_RetryJobsForwardsFileIDs(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/parser/jobs/retry": `{"retried":2}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("POST", "/v1/ai/parser/jobs/retry",
		strings.NewReader(`{"file_ids":["a","b"]}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.RetryJobs(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d, want 200", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "POST" ||
		(*captured)[0].Path != "/v1/parser/jobs/retry" ||
		!strings.Contains((*captured)[0].Body, `"file_ids":["a","b"]`) {
		t.Fatalf("captured = %+v", *captured)
	}
}

func TestParserProxy_GetAllowlistExtensions(t *testing.T) {
	srv, _, _ := startUpstream(t, map[string]string{
		"/v1/parser/allowlist/extensions": `{"extensions":[".pdf",".txt"]}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/parser/allowlist/extensions", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.GetAllowlistExtensions(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `".pdf"`) {
		t.Fatalf("body = %s", rec.Body.String())
	}
}

func TestParserProxy_PatchAllowlistExtension(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/parser/allowlist/extensions": `{"ok":true}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	reqBody := `{"ext":".docx","enabled":true}`
	req := httptest.NewRequest("PATCH", "/v1/ai/parser/allowlist/extensions",
		strings.NewReader(reqBody))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.PatchAllowlistExtension(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d, want 200", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "PATCH" ||
		!strings.Contains((*captured)[0].Body, `".docx"`) {
		t.Fatalf("captured = %+v", *captured)
	}
}

func TestParserProxy_GetAllowlistFolders(t *testing.T) {
	srv, _, _ := startUpstream(t, map[string]string{
		"/v1/parser/allowlist/folders": `{"folders":[{"id":1,"path":"/data/docs"}]}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/parser/allowlist/folders", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.GetAllowlistFolders(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"/data/docs"`) {
		t.Fatalf("body = %s", rec.Body.String())
	}
}

func TestParserProxy_PostAllowlistFolder(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/parser/allowlist/folders": `{"id":5,"path":"/data/new"}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	reqBody := `{"path":"/data/new"}`
	req := httptest.NewRequest("POST", "/v1/ai/parser/allowlist/folders",
		strings.NewReader(reqBody))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.PostAllowlistFolder(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"/data/new"`) {
		t.Fatalf("body = %s", rec.Body.String())
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "POST" ||
		!strings.Contains((*captured)[0].Body, `"/data/new"`) {
		t.Fatalf("captured = %+v", *captured)
	}
}

func TestParserProxy_DeleteAllowlistFolder(t *testing.T) {
	srv, captured, mu := startUpstream(t, nil)
	defer srv.Close()
	srv2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		*captured = append(*captured, capturedReq{r.Method, r.URL.Path, string(b)})
		mu.Unlock()
		w.WriteHeader(204)
	}))
	defer srv2.Close()
	p := newProxy(t, srv2.URL)
	e := echo.New()
	req := httptest.NewRequest("DELETE", "/v1/ai/parser/allowlist/folders/7", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues("7")
	if err := p.DeleteAllowlistFolder(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 204 {
		t.Fatalf("code = %d, want 204", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "DELETE" ||
		(*captured)[0].Path != "/v1/parser/allowlist/folders/7" {
		t.Fatalf("captured = %+v", *captured)
	}
}

func TestParserProxy_ReindexFilesForwardsBody(t *testing.T) {
	srv, captured, mu := startUpstream(t, map[string]string{
		"/v1/parser/files/reindex": `{"queued":2,"tombstoned":2,"job_ids":[1,2],"skipped":[]}`,
	})
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("POST", "/v1/ai/parser/files/reindex",
		strings.NewReader(`{"file_ids":["a","b"],"reason":"ui"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.ReindexFiles(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d, want 200", rec.Code)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(*captured) != 1 || (*captured)[0].Method != "POST" ||
		(*captured)[0].Path != "/v1/parser/files/reindex" ||
		!strings.Contains((*captured)[0].Body, `"file_ids":["a","b"]`) {
		t.Fatalf("captured = %+v", *captured)
	}
}

func TestParserProxy_ListFilesForwardsQueryString(t *testing.T) {
	var gotRawQuery string
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotRawQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"total":0,"limit":100,"offset":0,"files":[]}`))
	}))
	defer srv.Close()
	p := newProxy(t, srv.URL)
	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/parser/files?root_id=media&limit=50", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := p.ListFiles(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 {
		t.Fatalf("code = %d, want 200", rec.Code)
	}
	if gotPath != "/v1/parser/files" {
		t.Fatalf("upstream path = %q, want /v1/parser/files", gotPath)
	}
	if gotRawQuery != "root_id=media&limit=50" {
		t.Fatalf("upstream query = %q, want root_id=media&limit=50", gotRawQuery)
	}
}
