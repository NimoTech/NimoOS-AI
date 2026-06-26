package config

import (
	"os"
	"path/filepath"
	"testing"
)

func writeTempConf(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "ai.conf")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatalf("write temp conf: %v", err)
	}
	return p
}

// 这些测试用 Load(返回 *Config、不碰包级 Cfg)而非 Init,因此不共享全局状态,
// 可安全并行,也不会在测试间互相污染。

func TestOpenVINODefaultsWhenKeysAbsent(t *testing.T) {
	t.Parallel()
	// [openvino] 段不含 MaxLoadedModels / IdleTTLMinutes → 取默认 3 / 5。
	p := writeTempConf(t, "[openvino]\nURL = http://127.0.0.1:9100\nDevices = GPU.1\n")
	cfg, err := Load(p, "")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.OpenVINOMaxLoaded != 3 {
		t.Errorf("MaxLoaded = %d, want 3", cfg.OpenVINOMaxLoaded)
	}
	if cfg.OpenVINOIdleTTLMinutes != 5 {
		t.Errorf("IdleTTLMinutes = %d, want 5", cfg.OpenVINOIdleTTLMinutes)
	}
}

func TestOpenVINOExplicitZeroTTLAndMax(t *testing.T) {
	t.Parallel()
	// 显式 IdleTTLMinutes = 0(永不卸载)必须保留为 0,不被默认 5 覆盖;MaxLoadedModels 显式生效。
	p := writeTempConf(t, "[openvino]\nMaxLoadedModels = 2\nIdleTTLMinutes = 0\n")
	cfg, err := Load(p, "")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.OpenVINOMaxLoaded != 2 {
		t.Errorf("MaxLoaded = %d, want 2", cfg.OpenVINOMaxLoaded)
	}
	if cfg.OpenVINOIdleTTLMinutes != 0 {
		t.Errorf("IdleTTLMinutes = %d, want 0 (never unload)", cfg.OpenVINOIdleTTLMinutes)
	}
}
