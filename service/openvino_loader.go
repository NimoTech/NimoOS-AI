package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// AvailableModel is a discoverable OpenVINO model option. Listing it does NOT
// load it — loading happens on first use via EnsureLoaded (Ollama-style).
type AvailableModel struct {
	Display  string // user-facing name = original IR dir name, e.g. "qwen3.6-35b-a3b-int4"
	Device   string // e.g. "GPU.1"
	Servable string // OVMS internal servable name, e.g. "qwen3-6-35b-a3b-int4-gpu1"
}

// isOVModelDir reports whether dir holds OpenVINO IR (a model .xml).
func isOVModelDir(dir string) bool {
	for _, f := range []string{"openvino_language_model.xml", "openvino_model.xml"} {
		if _, err := os.Stat(filepath.Join(dir, f)); err == nil {
			return true
		}
	}
	return false
}

// AvailableModels scans srcModelsPath for IR model dirs and returns one option per
// (model, selectable-device). Pure filesystem scan — nothing is loaded.
func (a *OpenVINOAdapter) AvailableModels() []AvailableModel {
	entries, err := os.ReadDir(a.srcModelsPath)
	if err != nil {
		return nil
	}
	var out []AvailableModel
	for _, e := range entries {
		// 用 isOVModelDir(os.Stat 跟随软链)判断,不用 e.IsDir()——后者对"指向目录的
		// 符号链接"返回 false,会漏掉软链进来的模型。
		if !isOVModelDir(filepath.Join(a.srcModelsPath, e.Name())) {
			continue
		}
		for _, dev := range a.devices {
			out = append(out, AvailableModel{
				Display:  e.Name(),
				Device:   dev,
				Servable: OVMSModelName(e.Name(), dev),
			})
		}
	}
	return out
}

// EnsureLoaded makes sure the servable for (bareModel, device) is AVAILABLE in
// OVMS, loading it on demand and evicting any other loaded model (single-resident
// policy — VRAM holds one model at a time). Blocks until the model is ready or the
// load times out. Serialized: only one load happens at a time.
func (a *OpenVINOAdapter) EnsureLoaded(bareModel, device string) error {
	servable := OVMSModelName(bareModel, device)
	a.loadMu.Lock()
	defer a.loadMu.Unlock()

	if a.servableState(servable) == "AVAILABLE" {
		return nil // already loaded
	}
	if err := a.stageServable(bareModel, device, servable); err != nil {
		return err
	}
	if err := a.writeConfigSingle(servable); err != nil {
		return fmt.Errorf("write OVMS config: %w", err)
	}
	// OVMS polls config.json (file_system_poll_wait_seconds) and reloads to match:
	// it loads `servable` and unloads everything else. Wait for AVAILABLE.
	// "END" 是某 version 被卸载后的状态;重新载入时轮询可能瞬时读到它,故不作失败,
	// 只把 LOADING_FAILED 当终态失败,其余(""/LOADING/END)继续等到 AVAILABLE 或超时。
	deadline := time.Now().Add(5 * time.Minute)
	for time.Now().Before(deadline) {
		switch a.servableState(servable) {
		case "AVAILABLE":
			return nil
		case "LOADING_FAILED":
			return fmt.Errorf("openvino servable %q failed to load", servable)
		}
		time.Sleep(2 * time.Second)
	}
	return fmt.Errorf("openvino servable %q load timed out", servable)
}

// servableState returns the OVMS state of a servable ("" if absent/unreachable).
func (a *OpenVINOAdapter) servableState(servable string) string {
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(a.baseURL + "/v1/config")
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	var cfg map[string]struct {
		Status []struct {
			State string `json:"state"`
		} `json:"model_version_status"`
	}
	if json.NewDecoder(resp.Body).Decode(&cfg) != nil {
		return ""
	}
	if st, ok := cfg[servable]; ok && len(st.Status) > 0 {
		return st.Status[0].State
	}
	return ""
}

// stageServable lays out the OVMS servable dir (idempotent): repo/<servable>/
// with graph.pbtxt (device + Qwen3 parsers) and version dir 1/ symlinked to the
// raw IR under srcModelsPath/<bareModel>.
func (a *OpenVINOAdapter) stageServable(bareModel, device, servable string) error {
	src := filepath.Join(a.srcModelsPath, bareModel)
	if !isOVModelDir(src) {
		return fmt.Errorf("openvino model not found: %s", bareModel)
	}
	dir := filepath.Join(a.repoPath, servable)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	link := filepath.Join(dir, "1")
	_ = os.RemoveAll(link)
	if err := os.Symlink(src, link); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "graph.pbtxt"), []byte(ovmsGraphPbtxt(device)), 0o644)
}

type ovmsMediapipeEntry struct {
	Name     string `json:"name"`
	BasePath string `json:"base_path"`
}

type ovmsConfig struct {
	ModelConfigList     []any                `json:"model_config_list"`
	MediapipeConfigList []ovmsMediapipeEntry `json:"mediapipe_config_list"`
}

// writeConfigSingle rewrites OVMS config.json to load exactly one servable. OVMS's
// config poller picks up the change and loads it (unloading any others).
func (a *OpenVINOAdapter) writeConfigSingle(servable string) error {
	cfg := ovmsConfig{
		ModelConfigList: []any{},
		MediapipeConfigList: []ovmsMediapipeEntry{{
			Name:     servable,
			BasePath: filepath.Join(a.repoPath, servable),
		}},
	}
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(a.configPath), 0o755); err != nil {
		return err
	}
	return os.WriteFile(a.configPath, b, 0o644)
}

// ovmsGraphPbtxt builds the MediaPipe graph for a Qwen3 LLM servable on `device`,
// with the loopback back-edge + SyncSet handler OVMS requires, plus the Qwen3
// reasoning/tool parsers so <think>/<tool_call> are returned structured.
func ovmsGraphPbtxt(device string) string {
	return `input_stream: "HTTP_REQUEST_PAYLOAD:input"
output_stream: "HTTP_RESPONSE_PAYLOAD:output"
node: {
  name: "LLMExecutor"
  calculator: "HttpLLMCalculator"
  input_stream: "LOOPBACK:loopback"
  input_stream: "HTTP_REQUEST_PAYLOAD:input"
  input_side_packet: "LLM_NODE_RESOURCES:llm"
  output_stream: "LOOPBACK:loopback"
  output_stream: "HTTP_RESPONSE_PAYLOAD:output"
  input_stream_info: {
    tag_index: 'LOOPBACK:0',
    back_edge: true
  }
  node_options: {
    [type.googleapis.com/mediapipe.LLMCalculatorOptions]: {
      models_path: "./1",
      device: "` + device + `",
      plugin_config: '{"PERFORMANCE_HINT":"LATENCY"}',
      reasoning_parser: "qwen3",
      tool_parser: "qwen3coder"
    }
  }
  input_stream_handler {
    input_stream_handler: "SyncSetInputStreamHandler",
    options {
      [mediapipe.SyncSetInputStreamHandlerOptions.ext] {
        sync_set {
          tag_index: "LOOPBACK:0"
        }
      }
    }
  }
}
`
}
