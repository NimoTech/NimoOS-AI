package service

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestParserClient_GetStatePassesThrough(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/parser/control/state" {
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"paused":false,"concurrency":2}`))
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	dir := t.TempDir()
	discovery := filepath.Join(dir, "parser.url")
	if err := os.WriteFile(discovery, []byte(srv.URL), 0644); err != nil {
		t.Fatal(err)
	}

	pc := NewParserClient(discovery)
	body, status, err := pc.Get("/v1/parser/control/state")
	if err != nil {
		t.Fatalf("Get failed: %v", err)
	}
	if status != 200 {
		t.Fatalf("status = %d, want 200", status)
	}
	if string(body) != `{"paused":false,"concurrency":2}` {
		t.Fatalf("body = %s", body)
	}
}

func TestParserClient_RereadsDiscoveryOnConnError(t *testing.T) {
	dir := t.TempDir()
	discovery := filepath.Join(dir, "parser.url")
	// 先指向无效 URL
	os.WriteFile(discovery, []byte("http://127.0.0.1:1"), 0644)

	srvOK := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`ok`))
	}))
	defer srvOK.Close()

	pc := NewParserClient(discovery)
	// 第一次会失败 — 但失败时应重读 discovery,所以我们在调用 Get 前更新文件
	// 模拟实际场景:文件已更新,client 缓存还是旧的
	pc.SetCachedBaseURL("http://127.0.0.1:1") // 显式注入坏 cache
	os.WriteFile(discovery, []byte(srvOK.URL), 0644)

	body, status, err := pc.Get("/anything")
	if err != nil {
		t.Fatalf("Get with retry should succeed: %v", err)
	}
	if status != 200 || string(body) != "ok" {
		t.Fatalf("body = %s status = %d", body, status)
	}
}
