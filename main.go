package main

import (
	_ "embed"
	"flag"
	"fmt"
	"os"

	"github.com/NimoTech/NimoOS-AI/common"
	"github.com/NimoTech/NimoOS-AI/pkg/config"
)

var (
	commit = "private build"
	date   = "private build"

	//go:embed build/sysroot/etc/nimoos/ai.conf.sample
	_confSample string
)

func main() {
	configFlag := flag.String("c", "", "config file path")
	versionFlag := flag.Bool("v", false, "version")
	flag.Parse()

	if *versionFlag {
		fmt.Printf("v%s\n", common.AIVersion)
		os.Exit(0)
	}

	fmt.Println("git commit:", commit)
	fmt.Println("build date:", date)

	config.Init(*configFlag, _confSample)
	fmt.Println("NimoOS-AI starting... (stub)")
}
