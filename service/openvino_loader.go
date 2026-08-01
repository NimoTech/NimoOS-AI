package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/NimoTech/NimoOS-Common/utils/logger"
	"go.uber.org/zap"
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
		// Judge via isOVModelDir (os.Stat follows symlinks), not e.IsDir() — the
		// latter returns false for "a symlink pointing at a directory" and would
		// miss models brought in via a symlink.
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

// EnsureLoaded ensures the servable for (bareModel, device) is AVAILABLE in
// OVMS, loading it on demand. Multi-model residency: before loading a new
// model, if maxLoaded is already reached, evict the global LRU first; if
// loading fails (usually OOM), evict the least-recently-used entry on the same
// device and retry, until it fits or that device has nothing left to evict.
// If already resident and AVAILABLE, just renews the lease. Holds loadMu for
// the whole call (loads are serialized). Blocks until ready or timeout. Signature
// unchanged.
func (a *OpenVINOAdapter) EnsureLoaded(bareModel, device string) error {
	servable := OVMSModelName(bareModel, device)
	a.loadMu.Lock()
	defer a.loadMu.Unlock()

	// Already resident and OVMS reports AVAILABLE → renew the lease (keep-alive), no config change needed.
	if lm, ok := a.loaded[servable]; ok && a.servableState(servable) == "AVAILABLE" {
		lm.lastUsed = time.Now()
		return nil
	}

	if err := a.stageServable(bareModel, device, servable); err != nil {
		return err
	}

	// Count cap: before adding the new servable, if already at the cap, evict the global LRU.
	if _, ok := a.loaded[servable]; !ok {
		for a.maxLoaded > 0 && len(a.loaded) >= a.maxLoaded {
			victim, found := selectLRU(a.loaded, "", "")
			if !found {
				break
			}
			delete(a.loaded, victim)
		}
	}
	a.loaded[servable] = &loadedModel{servable: servable, device: device, lastUsed: time.Now()}

	// deadline is the total budget for the whole load flow (including multiple OOM
	// retries), not 5 minutes per attempt; deliberately bounded to avoid N retries
	// holding loadMu indefinitely and starving the idle-reap goroutine.
	deadline := time.Now().Add(5 * time.Minute)
	for {
		if err := a.writeConfigLocked(); err != nil {
			delete(a.loaded, servable)
			if rerr := a.writeConfigLocked(); rerr != nil {
				logger.Error("openvino: rollback config write failed", zap.String("servable", servable), zap.Error(rerr))
			}
			return fmt.Errorf("write OVMS config: %w", err)
		}
		switch a.waitState(servable, deadline) {
		case "AVAILABLE":
			a.loaded[servable].lastUsed = time.Now()
			return nil
		case "LOADING_FAILED":
			// Usually out of VRAM: evict the least-recently-used on the same device
			// (excluding the new servable itself), rewrite config, and retry.
			victim, found := selectLRU(a.loaded, device, servable)
			if !found {
				delete(a.loaded, servable)
				if rerr := a.writeConfigLocked(); rerr != nil {
					logger.Error("openvino: rollback config write failed", zap.String("servable", servable), zap.Error(rerr))
				}
				return fmt.Errorf("openvino servable %q failed to load (no same-device model to evict)", servable)
			}
			delete(a.loaded, victim)
			// Back to the top of the loop: write config with the victim removed and wait again.
		default: // timeout
			delete(a.loaded, servable)
			if rerr := a.writeConfigLocked(); rerr != nil {
				logger.Error("openvino: rollback config write failed", zap.String("servable", servable), zap.Error(rerr))
			}
			return fmt.Errorf("openvino servable %q load timed out", servable)
		}
	}
}

// waitState polls OVMS until the servable becomes AVAILABLE or LOADING_FAILED,
// or the deadline is hit. "END"/""/"LOADING" are treated as transitional states
// to keep waiting on. Returns the terminal state string; "" on timeout.
func (a *OpenVINOAdapter) waitState(servable string, deadline time.Time) string {
	for time.Now().Before(deadline) {
		switch a.servableState(servable) {
		case "AVAILABLE":
			return "AVAILABLE"
		case "LOADING_FAILED":
			return "LOADING_FAILED"
		}
		time.Sleep(2 * time.Second)
	}
	return ""
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

// ovmsGraphPbtxt builds the MediaPipe graph for a Qwen3 LLM servable on `device`,
// with the loopback back-edge + SyncSet handler OVMS requires, plus the Qwen3
// reasoning/tool parsers so <think>/<tool_call> are returned structured.
//
// enable_prefix_caching + dynamic_split_fuse + cache_size are performance-critical:
// agent mode injects the same large system prompt (tool + skill definitions)
// on every session, and tool calls trigger further rounds of LLM requests.
// Without prefix caching, every round has to prefill a several-thousand-token
// prefix from scratch (measured ~3s/round); with it enabled, prefill on the
// repeated prefix drops to ~0.2s (measured 15x speedup), and it hits across
// requests too. cache_size is in GB — 2GB leaves headroom on a B60 with 19GB
// of weights + 24GB of VRAM.
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
      enable_prefix_caching: true,
      dynamic_split_fuse: true,
      cache_size: 2,
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

// loadedModel records a currently-resident (written into config.json) servable,
// its device, and its last-used time.
type loadedModel struct {
	servable string
	device   string // used for "same-device LRU" eviction, e.g. "GPU.1"
	lastUsed time.Time
}

// selectLRU picks the servable in loaded with the earliest (least-recently-used)
// lastUsed. device=="" means no device restriction; otherwise only selects on
// that device. except is a servable name to exclude ("" = exclude none).
// Returns ("", false) when there's no candidate. Pure function, touches neither
// OVMS nor the filesystem.
func selectLRU(loaded map[string]*loadedModel, device, except string) (string, bool) {
	var pick string
	var pickTime time.Time
	found := false
	for name, m := range loaded {
		if name == except {
			continue
		}
		if device != "" && m.device != device {
			continue
		}
		if !found || m.lastUsed.Before(pickTime) {
			pick, pickTime, found = name, m.lastUsed, true
		}
	}
	return pick, found
}

// selectExpired returns the names of all servables in loaded that have been
// idle for at least ttl (now-lastUsed >= ttl), sorted ascending by name for
// determinism. ttl<=0 means "never expires", returns nil. Pure function.
func selectExpired(loaded map[string]*loadedModel, ttl time.Duration, now time.Time) []string {
	if ttl <= 0 {
		return nil
	}
	var out []string
	for name, m := range loaded {
		if now.Sub(m.lastUsed) >= ttl {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

// writeConfigLocked serializes the whole a.loaded set (sorted ascending by
// servable name) into config.json. OVMS's config poller then loads additions
// and unloads removals. Caller must already hold a.loadMu.
func (a *OpenVINOAdapter) writeConfigLocked() error {
	names := make([]string, 0, len(a.loaded))
	for name := range a.loaded {
		names = append(names, name)
	}
	sort.Strings(names)
	entries := make([]ovmsMediapipeEntry, 0, len(names))
	for _, name := range names {
		entries = append(entries, ovmsMediapipeEntry{
			Name:     name,
			BasePath: filepath.Join(a.repoPath, name),
		})
	}
	cfg := ovmsConfig{ModelConfigList: []any{}, MediapipeConfigList: entries}
	b, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(a.configPath), 0o755); err != nil {
		return err
	}
	return os.WriteFile(a.configPath, b, 0o644)
}

// reconcileFromOVMS rebuilds a.loaded at process startup from what OVMS is
// "actually serving right now", rather than blindly trusting config.json on
// disk. Reason: config.json persists across restarts, and after a full machine
// reboot OVMS would auto-load whatever models were resident last session going
// straight off that file — but that doesn't mean the user wants them loaded
// this time (and it can trigger an OOM loop during the boot-time memory spike).
// The real source of truth is "what OVMS is actually serving right now":
//   - full cold boot: the unit's ExecStartPre clears config before OVMS starts →
//     we find an empty set here → loaded stays empty, models mount only on
//     demand (EnsureLoaded).
//   - only nimoos-ai restarts (OVMS keeps running): we find OVMS's real resident
//     set here → correctly restored.
//
// Parses device back out of each one's graph.pbtxt (lastUsed=now, giving it a
// full TTL cycle). Best-effort: OVMS unreachable / not serving / parse failure
// → that entry is skipped. No locking needed (only called single-threaded, at
// construction time).
func (a *OpenVINOAdapter) reconcileFromOVMS() {
	now := time.Now()
	for _, name := range a.ListServedModels() {
		dev := a.parseDeviceFromGraph(name)
		if dev == "" {
			continue // can't recover the device → skip
		}
		a.loaded[name] = &loadedModel{servable: name, device: dev, lastUsed: now}
	}
}

// reapOnce scans loaded and unloads every servable that has been idle for at
// least idleTTL (mirrors Ollama keep_alive). idleTTL<=0 means never unload,
// returns immediately. Only rewrites config if something was removed. Holds loadMu.
func (a *OpenVINOAdapter) reapOnce() {
	a.loadMu.Lock()
	defer a.loadMu.Unlock()
	expired := selectExpired(a.loaded, a.idleTTL, time.Now())
	if len(expired) == 0 {
		return
	}
	for _, name := range expired {
		delete(a.loaded, name)
	}
	_ = a.writeConfigLocked()
}

// reaperLoop fires reapOnce once a minute; a long-lived background goroutine (started at construction time).
func (a *OpenVINOAdapter) reaperLoop() {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for range ticker.C {
		a.reapOnce()
	}
}

// parseDeviceFromGraph parses the device out of repoPath/<servable>/graph.pbtxt
// (written by this loader itself, always in the fixed format `device: "GPU.x"`).
// Returns "" if unreadable / unparsable.
func (a *OpenVINOAdapter) parseDeviceFromGraph(servable string) string {
	b, err := os.ReadFile(filepath.Join(a.repoPath, servable, "graph.pbtxt"))
	if err != nil {
		return ""
	}
	s := string(b)
	const marker = `device: "`
	i := strings.Index(s, marker)
	if i < 0 {
		return ""
	}
	rest := s[i+len(marker):]
	j := strings.Index(rest, `"`)
	if j < 0 {
		return ""
	}
	return rest[:j]
}
