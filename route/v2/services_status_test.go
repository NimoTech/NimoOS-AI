package v2

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

func writeDiscovery(t *testing.T, dir, name, target string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(target), 0644); err != nil {
		t.Fatal(err)
	}
	return p
}

// newMinimalAgentHandler returns an AgentHandler with available=false, without
// starting the health monitor (which would dial agentURL and never return).
func newMinimalAgentHandler() *AgentHandler {
	h := &AgentHandler{agentURL: "http://127.0.0.1:1"}
	h.available.Store(false)
	return h
}

func TestStatus_IncludesSearchAndParser(t *testing.T) {
	parserSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/parser/control/state":
			w.Write([]byte(`{"paused":false,"concurrency":2}`))
		case "/v1/parser/stats":
			w.Write([]byte(`{"queue_depth":{"pending":7,"running":1,"failed":0,"done":3},"indexed_files":3,"total_vectors_text":0,"total_vectors_visual":0,"last_cursor_ms":0,"models":[]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer parserSrv.Close()

	searchSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/search/_internal/health" {
			w.Write([]byte(`{"status":"ok"}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer searchSrv.Close()

	dir := t.TempDir()
	parserURL := writeDiscovery(t, dir, "parser.url", parserSrv.URL)
	searchURL := writeDiscovery(t, dir, "search.url", searchSrv.URL)

	svc := &ServicesStatusHandler{
		agentHandler: newMinimalAgentHandler(),
		ollamaURL:    "http://127.0.0.1:1", // dead — Ollama won't respond, but that's fine
		parserClient: service.NewParserClient(parserURL),
		searchClient: service.NewSearchClient(searchURL),
	}

	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/services/status", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	if err := svc.Status(c); err != nil {
		t.Fatalf("Status err: %v", err)
	}
	if rec.Code != 200 {
		t.Fatalf("HTTP code = %d, want 200", rec.Code)
	}

	var resp ServicesStatusResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if !resp.Search.Running {
		t.Errorf("Search.Running = false, want true")
	}
	if !resp.Parser.Running {
		t.Errorf("Parser.Running = false, want true")
	}
	if resp.Parser.Pending != 7 {
		t.Errorf("Parser.Pending = %d, want 7", resp.Parser.Pending)
	}
	if resp.Parser.Concurrency != 2 {
		t.Errorf("Parser.Concurrency = %d, want 2", resp.Parser.Concurrency)
	}
	if resp.Parser.Paused {
		t.Errorf("Parser.Paused = true, want false")
	}
}

func TestStatus_ParserDownReturnsRunningFalse(t *testing.T) {
	dir := t.TempDir()
	// Point at an unreachable address so the client returns an error quickly.
	parserURL := writeDiscovery(t, dir, "parser.url", "http://127.0.0.1:1")

	svc := &ServicesStatusHandler{
		agentHandler: newMinimalAgentHandler(),
		ollamaURL:    "http://127.0.0.1:1",
		parserClient: service.NewParserClient(parserURL),
	}

	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/services/status", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	t0 := time.Now()
	_ = svc.Status(c)
	dt := time.Since(t0)
	if dt > 2*time.Second {
		t.Fatalf("Status blocked %v, expected <2s due to 800ms timeout", dt)
	}

	var resp ServicesStatusResponse
	json.Unmarshal(rec.Body.Bytes(), &resp)
	if resp.Parser.Running {
		t.Errorf("Parser.Running = true, want false (parser unreachable)")
	}
}

func TestStatus_SearchDownReturnsRunningFalse(t *testing.T) {
	dir := t.TempDir()
	searchURL := writeDiscovery(t, dir, "search.url", "http://127.0.0.1:1")

	svc := &ServicesStatusHandler{
		agentHandler: newMinimalAgentHandler(),
		ollamaURL:    "http://127.0.0.1:1",
		searchClient: service.NewSearchClient(searchURL),
	}

	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/services/status", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	_ = svc.Status(c)

	var resp ServicesStatusResponse
	json.Unmarshal(rec.Body.Bytes(), &resp)
	if resp.Search.Running {
		t.Errorf("Search.Running = true, want false (search unreachable)")
	}
}

func TestStatus_NilClientsReturnFalse(t *testing.T) {
	svc := &ServicesStatusHandler{
		agentHandler: newMinimalAgentHandler(),
		ollamaURL:    "http://127.0.0.1:1",
		parserClient: nil,
		searchClient: nil,
	}

	e := echo.New()
	req := httptest.NewRequest("GET", "/v1/ai/services/status", nil)
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)

	if err := svc.Status(c); err != nil {
		t.Fatalf("Status err: %v", err)
	}

	var resp ServicesStatusResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Parser.Running {
		t.Errorf("Parser.Running should be false when client is nil")
	}
	if resp.Search.Running {
		t.Errorf("Search.Running should be false when client is nil")
	}
}
