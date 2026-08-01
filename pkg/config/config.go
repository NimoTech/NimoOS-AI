package config

import (
	"fmt"
	"os"

	"github.com/spf13/viper"
)

var Cfg *Config

type Config struct {
	RuntimePath            string
	DataPath               string
	MasterKeyPath          string
	LogPath                string
	AgentURL               string
	AgentTimeout           int
	OllamaURL              string
	OpenVINOURL            string
	OpenVINOEnabled        bool
	OpenVINODevices        string // comma-separated resident devices, e.g. "GPU.1" or "GPU.1,GPU.0"; the first is the default device
	OpenVINOMaxLoaded      int    // max number of models resident at once (mirrors Ollama OLLAMA_MAX_LOADED_MODELS), default 3
	OpenVINOIdleTTLMinutes int    // minutes of idleness before auto-unload (mirrors Ollama keep_alive), default 5; 0 = never unload
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
		RuntimePath:            v.GetString("common.RuntimePath"),
		DataPath:               v.GetString("common.DataPath"),
		MasterKeyPath:          v.GetString("ai.MasterKeyPath"),
		LogPath:                v.GetString("common.LogPath"),
		AgentURL:               v.GetString("agent.AgentURL"),
		AgentTimeout:           v.GetInt("agent.AgentTimeout"),
		OllamaURL:              v.GetString("agent.OllamaURL"),
		OpenVINOURL:            v.GetString("openvino.URL"),
		OpenVINOEnabled:        v.GetBool("openvino.Enabled"),
		OpenVINODevices:        v.GetString("openvino.Devices"),
		OpenVINOMaxLoaded:      v.GetInt("openvino.MaxLoadedModels"),
		OpenVINOIdleTTLMinutes: v.GetInt("openvino.IdleTTLMinutes"),
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
	// Enabled defaults to true: only use the configured value if openvino.Enabled is explicitly set, otherwise true.
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
	// IdleTTLMinutes: an explicit 0 means never unload, and must be distinguished from "key missing" — only use the default of 5 when unset.
	if !v.IsSet("openvino.IdleTTLMinutes") {
		cfg.OpenVINOIdleTTLMinutes = 5
	}
	return cfg, nil
}
