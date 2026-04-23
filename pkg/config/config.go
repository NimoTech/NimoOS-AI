package config

import (
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

func Init(configFile, confSample string) {
	if configFile == "" {
		configFile = "/etc/nimoos/ai.conf"
	}
	if _, err := os.Stat(configFile); os.IsNotExist(err) {
		_ = os.WriteFile(configFile, []byte(confSample), 0644)
	}

	v := viper.New()
	v.SetConfigFile(configFile)
	v.SetConfigType("ini")
	_ = v.ReadInConfig()

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
}
