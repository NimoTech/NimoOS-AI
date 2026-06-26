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

func TestOpenVINODefaultsWhenKeysAbsent(t *testing.T) {
	// [openvino] 段不含 MaxLoadedModels / IdleTTLMinutes → 取默认 3 / 5。
	p := writeTempConf(t, "[openvino]\nURL = http://127.0.0.1:9100\nDevices = GPU.1\n")
	if err := Init(p, ""); err != nil {
		t.Fatalf("Init: %v", err)
	}
	if Cfg.OpenVINOMaxLoaded != 3 {
		t.Errorf("MaxLoaded = %d, want 3", Cfg.OpenVINOMaxLoaded)
	}
	if Cfg.OpenVINOIdleTTLMinutes != 5 {
		t.Errorf("IdleTTLMinutes = %d, want 5", Cfg.OpenVINOIdleTTLMinutes)
	}
}

func TestOpenVINOExplicitZeroTTLAndMax(t *testing.T) {
	// 显式 IdleTTLMinutes = 0(永不卸载)必须保留为 0,不被默认 5 覆盖;MaxLoadedModels 显式生效。
	p := writeTempConf(t, "[openvino]\nMaxLoadedModels = 2\nIdleTTLMinutes = 0\n")
	if err := Init(p, ""); err != nil {
		t.Fatalf("Init: %v", err)
	}
	if Cfg.OpenVINOMaxLoaded != 2 {
		t.Errorf("MaxLoaded = %d, want 2", Cfg.OpenVINOMaxLoaded)
	}
	if Cfg.OpenVINOIdleTTLMinutes != 0 {
		t.Errorf("IdleTTLMinutes = %d, want 0 (never unload)", Cfg.OpenVINOIdleTTLMinutes)
	}
}
