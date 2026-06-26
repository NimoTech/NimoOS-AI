package service

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
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

	// 全局 LRU:c-gpu0 最早(base-1m)。
	if got, ok := selectLRU(loaded, "", ""); !ok || got != "c-gpu0" {
		t.Errorf("global LRU = %q,%v; want c-gpu0,true", got, ok)
	}
	// 限 GPU.1:a-gpu1 最早。
	if got, ok := selectLRU(loaded, "GPU.1", ""); !ok || got != "a-gpu1" {
		t.Errorf("GPU.1 LRU = %q,%v; want a-gpu1,true", got, ok)
	}
	// 限 GPU.1 且排除 a-gpu1 → 退到 b-gpu1。
	if got, ok := selectLRU(loaded, "GPU.1", "a-gpu1"); !ok || got != "b-gpu1" {
		t.Errorf("GPU.1 LRU except a = %q,%v; want b-gpu1,true", got, ok)
	}
	// 该设备上排除唯一项 → 无候选。
	if got, ok := selectLRU(loaded, "GPU.0", "c-gpu0"); ok {
		t.Errorf("GPU.0 LRU except c = %q,%v; want \"\",false", got, ok)
	}
	// 空集 → 无候选。
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
	// ttl=5m:stale(10m)与 edge(恰好 5m,>= 命中)过期;fresh(1m)不过期。按名字升序。
	got := selectExpired(loaded, 5*time.Minute, now)
	want := []string{"edge-gpu0", "stale-gpu1"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("selectExpired = %v; want %v", got, want)
	}
	// ttl=0 → 永不过期。
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
	// 按名升序:a-gpu0 在前,b-gpu1 在后。
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
	// config.json 应只剩 fresh-gpu1。
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
		idleTTL:    0, // 永不卸载
		loaded: map[string]*loadedModel{
			"old-gpu1": {servable: "old-gpu1", device: "GPU.1", lastUsed: time.Now().Add(-time.Hour)},
		},
	}
	a.reapOnce()
	if _, ok := a.loaded["old-gpu1"]; !ok {
		t.Error("ttl=0 must never reap")
	}
}

func TestReconcileFromConfig(t *testing.T) {
	dir := t.TempDir()
	repo := filepath.Join(dir, "repo")
	// 造两个 servable 的 graph.pbtxt(含 device),外加一个缺 graph 的坏条目。
	for name, dev := range map[string]string{"x-gpu1": "GPU.1", "y-gpu0": "GPU.0"} {
		d := filepath.Join(repo, name)
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, "graph.pbtxt"), []byte(ovmsGraphPbtxt(dev)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	cfgPath := filepath.Join(dir, "config.json")
	cfg := ovmsConfig{
		ModelConfigList: []any{},
		MediapipeConfigList: []ovmsMediapipeEntry{
			{Name: "x-gpu1", BasePath: filepath.Join(repo, "x-gpu1")},
			{Name: "y-gpu0", BasePath: filepath.Join(repo, "y-gpu0")},
			{Name: "missing-gpu1", BasePath: filepath.Join(repo, "missing-gpu1")}, // 无 graph → 跳过
		},
	}
	cb, _ := json.MarshalIndent(cfg, "", "  ")
	if err := os.WriteFile(cfgPath, cb, 0o644); err != nil {
		t.Fatal(err)
	}

	a := &OpenVINOAdapter{repoPath: repo, configPath: cfgPath, loaded: map[string]*loadedModel{}}
	a.reconcileFromConfig()

	if len(a.loaded) != 2 {
		t.Fatalf("loaded = %d, want 2 (bad entry skipped)", len(a.loaded))
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
}
