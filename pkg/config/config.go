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
	return nil
}
