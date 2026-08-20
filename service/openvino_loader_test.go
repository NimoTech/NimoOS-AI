package service

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestSelectLRU(t *testing.T) {
	base := time.Date(2026, 6, 26, 12, 0, 0, 0, time.UTC)
	loaded := map[string]*loadedModel{
		"a-gpu1": {servable: "a-gpu1", device: "GPU.1", lastUsed: base},
		"b-gpu1": {servable: "b-gpu1", device: "GPU.1", lastUsed: base.Add(2 * time.Minute)},
		"c-gpu0": {servable: "c-gpu0", device: "GPU.0", lastUsed: base.Add(-time.Minute)},
	}

	// global LRU: c-gpu0 is earliest (base-1m).
	if got, ok := selectLRU(loaded, "", ""); !ok || got != "c-gpu0" {
		t.Errorf("global LRU = %q,%v; want c-gpu0,true", got, ok)
	}
	// restricted to GPU.1: a-gpu1 is earliest.
	if got, ok := selectLRU(loaded, "GPU.1", ""); !ok || got != "a-gpu1" {
		t.Errorf("GPU.1 LRU = %q,%v; want a-gpu1,true", got, ok)
	}
	// restricted to GPU.1 and excluding a-gpu1 → falls back to b-gpu1.
	if got, ok := selectLRU(loaded, "GPU.1", "a-gpu1"); !ok || got != "b-gpu1" {
		t.Errorf("GPU.1 LRU except a = %q,%v; want b-gpu1,true", got, ok)
	}
	// excluding the only entry on that device → no candidate.
	if got, ok := selectLRU(loaded, "GPU.0", "c-gpu0"); ok {
		t.Errorf("GPU.0 LRU except c = %q,%v; want \"\",false", got, ok)
	}
	// empty set → no candidate.
	if _, ok := selectLRU(map[string]*loadedModel{}, "", ""); ok {
		t.Error("empty map should return ok=false")
	}
}

func TestSelectExpired(t *testing.T) {
	now := time.Date(2026, 6, 26, 12, 0, 0, 0, time.UTC)
	loaded := map[string]*loadedModel{
		"fresh-gpu1": {servable: "fresh-gpu1", device: "GPU.1", lastUsed: now.Add(-1 * time.Minute)},
		"stale-gpu1": {servable: "stale-gpu1", device: "GPU.1", lastUsed: now.Add(-10 * time.Minute)},
		"edge-gpu0":  {servable: "edge-gpu0", device: "GPU.0", lastUsed: now.Add(-5 * time.Minute)},
	}
	// ttl=5m: stale (10m) and edge (exactly 5m, >= hits) are expired; fresh (1m) is not. Ascending by name.
	got := selectExpired(loaded, 5*time.Minute, now)
	want := []string{"edge-gpu0", "stale-gpu1"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("selectExpired = %v; want %v", got, want)
	}
	// ttl=0 → never expires.
	if got := selectExpired(loaded, 0, now); got != nil {
		t.Errorf("ttl=0 should return nil, got %v", got)
	}
}

func TestWriteConfigLocked(t *testing.T) {
	dir := t.TempDir()
	a := &OpenVINOAdapter{
		repoPath:   filepath.Join(dir, "repo"),
		configPath: filepath.Join(dir, "config.json"),
		loaded: map[string]*loadedModel{
			"b-gpu1": {servable: "b-gpu1", device: "GPU.1", lastUsed: time.Now()},
			"a-gpu0": {servable: "a-gpu0", device: "GPU.0", lastUsed: time.Now()},
		},
	}
	if err := a.writeConfigLocked(); err != nil {
		t.Fatalf("writeConfigLocked: %v", err)
	}
	b, err := os.ReadFile(a.configPath)
	if err != nil {
		t.Fatalf("read config: %v", err)
	}
	var cfg ovmsConfig
	if err := json.Unmarshal(b, &cfg); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(cfg.MediapipeConfigList) != 2 {
		t.Fatalf("entries = %d, want 2", len(cfg.MediapipeConfigList))
	}
	// ascending by name: a-gpu0 first, b-gpu1 second.
	if cfg.MediapipeConfigList[0].Name != "a-gpu0" || cfg.MediapipeConfigList[1].Name != "b-gpu1" {
		t.Errorf("order = %q,%q; want a-gpu0,b-gpu1",
			cfg.MediapipeConfigList[0].Name, cfg.MediapipeConfigList[1].Name)
	}
	if cfg.MediapipeConfigList[0].BasePath != filepath.Join(a.repoPath, "a-gpu0") {
		t.Errorf("base_path = %q", cfg.MediapipeConfigList[0].BasePath)
	}
}

func TestReapOnce(t *testing.T) {
	dir := t.TempDir()
	a := &OpenVINOAdapter{
		repoPath:   filepath.Join(dir, "repo"),
		configPath: filepath.Join(dir, "config.json"),
		idleTTL:    5 * time.Minute,
		loaded: map[string]*loadedModel{
			"fresh-gpu1": {servable: "fresh-gpu1", device: "GPU.1", lastUsed: time.Now()},
			"stale-gpu1": {servable: "stale-gpu1", device: "GPU.1", lastUsed: time.Now().Add(-10 * time.Minute)},
		},
	}
	a.reapOnce()

	if _, ok := a.loaded["stale-gpu1"]; ok {
		t.Error("stale-gpu1 should have been reaped")
	}
	if _, ok := a.loaded["fresh-gpu1"]; !ok {
		t.Error("fresh-gpu1 should remain")
	}
	// config.json should only have fresh-gpu1 left.
	b, err := os.ReadFile(a.configPath)
	if err != nil {
		t.Fatalf("read config: %v", err)
	}
	var cfg ovmsConfig
	if err := json.Unmarshal(b, &cfg); err != nil {
		t.Fatal(err)
	}
	if len(cfg.MediapipeConfigList) != 1 || cfg.MediapipeConfigList[0].Name != "fresh-gpu1" {
		t.Errorf("config after reap = %+v; want only fresh-gpu1", cfg.MediapipeConfigList)
	}
}

func TestReapOnceNeverWhenTTLZero(t *testing.T) {
	dir := t.TempDir()
	a := &OpenVINOAdapter{
		repoPath:   filepath.Join(dir, "repo"),
		configPath: filepath.Join(dir, "config.json"),
		idleTTL:    0, // never unload
		loaded: map[string]*loadedModel{
			"old-gpu1": {servable: "old-gpu1", device: "GPU.1", lastUsed: time.Now().Add(-time.Hour)},
		},
	}
	a.reapOnce()
	if _, ok := a.loaded["old-gpu1"]; !ok {
		t.Error("ttl=0 must never reap")
	}
}

// reconcile rebuilds from OVMS's live serving set (servables with
// state=AVAILABLE in /v1/config), no longer reading config.json off disk.
// This way, after a full machine reboot (OVMS starts cleared by ExecStartPre
// → live serving set is empty), the previous models are not resurrected;
// they're only restored when nimoos-ai alone restarts while OVMS stays resident.
func TestReconcileFromOVMS(t *testing.T) {
	dir := t.TempDir()
	repo := filepath.Join(dir, "repo")
	// Build graph.pbtxt (with device) for two servables. missing-gpu1 deliberately has no graph built → should be skipped.
	for name, dev := range map[string]string{"x-gpu1": "GPU.1", "y-gpu0": "GPU.0"} {
		d := filepath.Join(repo, name)
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, "graph.pbtxt"), []byte(ovmsGraphPbtxt(dev)), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	// Simulate OVMS /v1/config: x-gpu1 / y-gpu0 / missing-gpu1 are all AVAILABLE;
	// loading-gpu1 is in LOADING (not AVAILABLE) → ListServedModels does not return it.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/config" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(`{
			"x-gpu1":        {"model_version_status":[{"state":"AVAILABLE"}]},
			"y-gpu0":        {"model_version_status":[{"state":"AVAILABLE"}]},
			"missing-gpu1":  {"model_version_status":[{"state":"AVAILABLE"}]},
			"loading-gpu1":  {"model_version_status":[{"state":"LOADING"}]}
		}`))
	}))
	defer srv.Close()

	a := &OpenVINOAdapter{baseURL: srv.URL, repoPath: repo, loaded: map[string]*loadedModel{}}
	a.reconcileFromOVMS()

	if len(a.loaded) != 2 {
		t.Fatalf("loaded = %d, want 2 (missing-gpu1 skipped for no graph, loading-gpu1 skipped for not AVAILABLE)", len(a.loaded))
	}
	if a.loaded["x-gpu1"] == nil || a.loaded["x-gpu1"].device != "GPU.1" {
		t.Errorf("x-gpu1 device wrong: %+v", a.loaded["x-gpu1"])
	}
	if a.loaded["y-gpu0"] == nil || a.loaded["y-gpu0"].device != "GPU.0" {
		t.Errorf("y-gpu0 device wrong: %+v", a.loaded["y-gpu0"])
	}
	if _, ok := a.loaded["missing-gpu1"]; ok {
		t.Error("missing-gpu1 should be skipped (no graph.pbtxt)")
	}
	if _, ok := a.loaded["loading-gpu1"]; ok {
		t.Error("loading-gpu1 should be skipped (not AVAILABLE)")
	}
}

// Full cold-boot scenario: OVMS's live serving set is empty → after reconcile, loaded must be empty (no resurrecting old models).
func TestReconcileFromOVMSEmptyOnFreshBoot(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	a := &OpenVINOAdapter{baseURL: srv.URL, repoPath: t.TempDir(), loaded: map[string]*loadedModel{}}
	a.reconcileFromOVMS()

	if len(a.loaded) != 0 {
		t.Fatalf("fresh boot loaded = %d, want 0 (must never resurrect old models)", len(a.loaded))
	}
}

// The graph template must request dynamic KV cache allocation (cache_size 0:
// GenAI grows the cache on demand from actual free VRAM, verified on both
// OVMS 2026.2.1 and 2026.4.0) and 8-bit KV quantization via the GPU plugin
// property KV_CACHE_PRECISION (a top-level kv_cache_precision graph field
// does not exist in LLMCalculatorOptions and fails graph parsing).
func TestOvmsGraphPbtxtDynamicU8KVCache(t *testing.T) {
	g := ovmsGraphPbtxt("GPU.1")
	if !strings.Contains(g, "cache_size: 0,") {
		t.Errorf("graph must set cache_size: 0 (dynamic), got:\n%s", g)
	}
	if !strings.Contains(g, `"KV_CACHE_PRECISION":"u8"`) {
		t.Errorf("plugin_config must set KV_CACHE_PRECISION u8, got:\n%s", g)
	}
	if strings.Contains(g, "kv_cache_precision") {
		t.Errorf("kv_cache_precision must not appear as a graph field (unsupported by LLMCalculatorOptions)")
	}
	if !strings.Contains(g, `device: "GPU.1"`) {
		t.Errorf("device must still be templated, got:\n%s", g)
	}
}
