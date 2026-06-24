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
}

func Init(configFile, confSample string) error {
	if configFile == "" {
		configFile = "/etc/nimoos/ai.conf"
	}
	if _, err := os.Stat(configFile); os.IsNotExist(err) {
		if err := os.WriteFile(configFile, []byte(confSample), 0644); err != nil {
			return fmt.Errorf("failed to write default config: %w", err)
		}
	}

	v := viper.New()
	v.SetConfigFile(configFile)
	v.SetConfigType("ini")
	if err := v.ReadInConfig(); err != nil {
		return fmt.Errorf("failed to read config: %w", err)
	}

	Cfg = &Config{
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
	}

	if Cfg.RuntimePath == "" {
		Cfg.RuntimePath = "/var/run/nimoos"
	}
	if Cfg.DataPath == "" {
		Cfg.DataPath = "/var/lib/nimoos/ai"
	}
	if Cfg.MasterKeyPath == "" {
		Cfg.MasterKeyPath = "/etc/nimoos/ai_master.key"
	}
	if Cfg.LogPath == "" {
		Cfg.LogPath = "/var/log/nimoos"
	}
	if Cfg.AgentURL == "" {
		Cfg.AgentURL = "http://127.0.0.1:8282"
	}
	if Cfg.AgentTimeout == 0 {
		Cfg.AgentTimeout = 60
	}
	if Cfg.OllamaURL == "" {
		Cfg.OllamaURL = "http://127.0.0.1:11434"
	}
	if Cfg.OpenVINOURL == "" {
		Cfg.OpenVINOURL = "http://127.0.0.1:9100"
	}
	// Enabled 默认 true:仅当配置里显式写了 openvino.Enabled 才用其值,否则 true。
	if v.IsSet("openvino.Enabled") {
		Cfg.OpenVINOEnabled = v.GetBool("openvino.Enabled")
	} else {
		Cfg.OpenVINOEnabled = true
	}
	if Cfg.OpenVINODevices == "" {
		Cfg.OpenVINODevices = "GPU.1"
	}
	return nil
}
