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

// These tests use Load (returns *Config, does not touch the package-level Cfg)
// instead of Init, so they share no global state, can run in parallel safely,
// and don't contaminate each other.

func TestOpenVINODefaultsWhenKeysAbsent(t *testing.T) {
	t.Parallel()
	// [openvino] section has no MaxLoadedModels / IdleTTLMinutes → falls back to default 3 / 5.
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
	// An explicit IdleTTLMinutes = 0 (never unload) must stay 0, not get overridden by the default 5; MaxLoadedModels takes its explicit value.
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
