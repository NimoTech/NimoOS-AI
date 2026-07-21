package config

import (
	"fmt"
	"os"

	"github.com/spf13/viper"
)

var Cfg *Config

type Config struct {
	RuntimePath   string
	DataPath      string
	MasterKeyPath string
	LogPath       string
	AgentURL        string
	AgentTimeout    int
	OllamaURL       string
	OpenVINOURL     string
	OpenVINOEnabled bool
	OpenVINODevices string // 逗号分隔的常驻设备,如 "GPU.1" 或 "GPU.1,GPU.0";第一个为默认设备
	OpenVINOMaxLoaded      int // 同时最多驻留的模型数(对齐 Ollama OLLAMA_MAX_LOADED_MODELS),默认 3
	OpenVINOIdleTTLMinutes int // 空闲多少分钟后自动卸载(对齐 Ollama keep_alive),默认 5;0=永不卸载
	OpenVINOCacheSizeGB    int // 每个 servable 的 KV cache 池大小(GB,OVMS cache_size),默认 2
}

// Init loads the config and assigns it to the package-level Cfg. It is the
// production entry point; tests should call Load instead so they don't mutate
// (or race on) the global.
func Init(configFile, confSample string) error {
	cfg, err := Load(configFile, confSample)
	if err != nil {
		return err
	}
	Cfg = cfg
	return nil
}

// Load reads and parses the config file (writing confSample when the file is
// absent) and returns a fully-defaulted *Config. It does NOT touch the
// package-level Cfg, so callers and tests can build configs without shared
// global state.
func Load(configFile, confSample string) (*Config, error) {
	if configFile == "" {
		configFile = "/etc/nimoos/ai.conf"
	}
	if _, err := os.Stat(configFile); os.IsNotExist(err) {
		if err := os.WriteFile(configFile, []byte(confSample), 0644); err != nil {
			return nil, fmt.Errorf("failed to write default config: %w", err)
		}
	}

	v := viper.New()
	v.SetConfigFile(configFile)
	v.SetConfigType("ini")
	if err := v.ReadInConfig(); err != nil {
		return nil, fmt.Errorf("failed to read config: %w", err)
	}

	cfg := &Config{
		RuntimePath:   v.GetString("common.RuntimePath"),
		DataPath:      v.GetString("common.DataPath"),
		MasterKeyPath: v.GetString("ai.MasterKeyPath"),
		LogPath:       v.GetString("common.LogPath"),
		AgentURL:        v.GetString("agent.AgentURL"),
		AgentTimeout:    v.GetInt("agent.AgentTimeout"),
		OllamaURL:       v.GetString("agent.OllamaURL"),
		OpenVINOURL:     v.GetString("openvino.URL"),
		OpenVINOEnabled: v.GetBool("openvino.Enabled"),
		OpenVINODevices: v.GetString("openvino.Devices"),
		OpenVINOMaxLoaded:      v.GetInt("openvino.MaxLoadedModels"),
		OpenVINOIdleTTLMinutes: v.GetInt("openvino.IdleTTLMinutes"),
		OpenVINOCacheSizeGB:    v.GetInt("openvino.CacheSizeGB"),
	}

	if cfg.RuntimePath == "" {
		cfg.RuntimePath = "/var/run/nimoos"
	}
	if cfg.DataPath == "" {
		cfg.DataPath = "/var/lib/nimoos/ai"
	}
	if cfg.MasterKeyPath == "" {
		cfg.MasterKeyPath = "/etc/nimoos/ai_master.key"
	}
	if cfg.LogPath == "" {
		cfg.LogPath = "/var/log/nimoos"
	}
	if cfg.AgentURL == "" {
		cfg.AgentURL = "http://127.0.0.1:8282"
	}
	if cfg.AgentTimeout == 0 {
		cfg.AgentTimeout = 60
	}
	if cfg.OllamaURL == "" {
		cfg.OllamaURL = "http://127.0.0.1:11434"
	}
	if cfg.OpenVINOURL == "" {
		cfg.OpenVINOURL = "http://127.0.0.1:9100"
	}
	// Enabled 默认 true:仅当配置里显式写了 openvino.Enabled 才用其值,否则 true。
	if v.IsSet("openvino.Enabled") {
		cfg.OpenVINOEnabled = v.GetBool("openvino.Enabled")
	} else {
		cfg.OpenVINOEnabled = true
	}
	if cfg.OpenVINODevices == "" {
		cfg.OpenVINODevices = "GPU.1"
	}
	if cfg.OpenVINOMaxLoaded <= 0 {
		cfg.OpenVINOMaxLoaded = 3
	}
	// IdleTTLMinutes:显式 0 = 永不卸载,必须与"键缺失"区分 —— 仅当未设置时才取默认 5。
	if !v.IsSet("openvino.IdleTTLMinutes") {
		cfg.OpenVINOIdleTTLMinutes = 5
	}
	if cfg.OpenVINOCacheSizeGB <= 0 {
		cfg.OpenVINOCacheSizeGB = 2
	}
	return cfg, nil
}
