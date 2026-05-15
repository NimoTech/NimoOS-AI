package main

import (
	"context"
	_ "embed"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/NimoTech/NimoOS-AI/common"
	"github.com/NimoTech/NimoOS-AI/pkg/config"
	"github.com/NimoTech/NimoOS-AI/route"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/NimoTech/NimoOS-Common/external"
	"github.com/NimoTech/NimoOS-Common/model"
	"github.com/NimoTech/NimoOS-Common/utils/file"
	"github.com/NimoTech/NimoOS-Common/utils/logger"
	"github.com/coreos/go-systemd/daemon"
	"go.uber.org/zap"
)

var (
	commit = "private build"
	date   = "private build"

	//go:embed build/sysroot/etc/nimoos/ai.conf.sample
	_confSample string
)

func main() {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	configFlag := flag.String("c", "", "config file path")
	versionFlag := flag.Bool("v", false, "version")
	flag.Parse()

	if *versionFlag {
		fmt.Printf("v%s\n", common.AIVersion)
		os.Exit(0)
	}

	fmt.Println("git commit:", commit)
	fmt.Println("build date:", date)

	if err := config.Init(*configFlag, _confSample); err != nil {
		fmt.Fprintf(os.Stderr, "failed to initialize config: %v\n", err)
		os.Exit(1)
	}

	if err := os.MkdirAll(config.Cfg.DataPath, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "failed to create data directory: %v\n", err)
		os.Exit(1)
	}

	logger.LogInit(config.Cfg.LogPath, "nimoos-ai", "log")

	svc := service.NewService(config.Cfg)

	// Start Ollama health checker
	go func() {
		checker := svc.OllamaChecker()
		checker.SetCallbacks(
			func() { logger.Info("Ollama is unhealthy after 3 consecutive failures") },
			func() { logger.Info("Ollama recovered") },
		)
		checker.Start(ctx)
	}()

	// Bind to a random port on localhost
	listener, err := net.Listen("tcp", net.JoinHostPort(common.Localhost, "0"))
	if err != nil {
		panic("failed to listen: " + err.Error())
	}

	// Write URL file for service discovery
	urlFilePath := filepath.Join(config.Cfg.RuntimePath, common.URLFileName)
	if err := file.CreateFileAndWriteContent(urlFilePath, "http://"+listener.Addr().String()); err != nil {
		logger.Error("failed to write URL file", zap.Error(err))
		// URL file failure is non-fatal: Gateway registration below sends the address directly.
		// Other consumers that read the URL file may fail to discover this service.
	}

	// Register routes at Gateway
	gw, err := external.NewManagementService(config.Cfg.RuntimePath)
	if err != nil {
		panic("failed to connect to Gateway: " + err.Error())
	}
	for _, path := range []string{common.V2APIPath, common.V2DocPath} {
		if err := gw.CreateRoute(&model.Route{
			Path:   path,
			Target: "http://" + listener.Addr().String(),
		}); err != nil {
			panic("failed to register route " + path + ": " + err.Error())
		}
	}

	handler := route.InitV2Router(svc, config.Cfg.RuntimePath, config.Cfg.AgentURL, config.Cfg.OllamaURL)

	// Notify systemd
	if supported, err := daemon.SdNotify(false, daemon.SdNotifyReady); err != nil {
		logger.Error("failed to notify systemd", zap.Error(err))
	} else if supported {
		logger.Info("notified systemd: ready")
	}

	logger.Info("NimoOS-AI listening", zap.String("address", listener.Addr().String()))

	s := &http.Server{
		Handler: handler,
		// Increased from 5s to 30s — multipart-upload routes (/agent/sessions/*/attachments)
		// can spend longer reading their headers under slow networks. The body itself
		// is intentionally unbounded so 500MB uploads can complete; only header read
		// is gated here. The /agent/* reverse proxy uses the default Transport, which
		// also has no body-read timeout.
		ReadHeaderTimeout: 30 * time.Second,
	}
	if err := s.Serve(listener); err != nil && err != http.ErrServerClosed {
		logger.Error("server error", zap.Error(err))
	}
}
