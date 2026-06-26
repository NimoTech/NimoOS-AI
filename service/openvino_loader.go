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

// EnsureLoaded 确保 (bareModel, device) 的 servable 在 OVMS 中 AVAILABLE,按需加载。
// 多模型驻留:加载新模型前若已达 maxLoaded,先踢全局 LRU;加载若失败(多半 OOM),按同
// 设备 LRU 踢最久未用的并重试,直至装下或该设备无可踢。已驻留且 AVAILABLE 则只续期。
// 全程持 loadMu(加载串行)。阻塞直到就绪或超时。签名保持不变。
func (a *OpenVINOAdapter) EnsureLoaded(bareModel, device string) error {
	servable := OVMSModelName(bareModel, device)
	a.loadMu.Lock()
	defer a.loadMu.Unlock()

	// 已驻留且 OVMS 报 AVAILABLE → 续期(keep-alive),无需改 config。
	if lm, ok := a.loaded[servable]; ok && a.servableState(servable) == "AVAILABLE" {
		lm.lastUsed = time.Now()
		return nil
	}

	if err := a.stageServable(bareModel, device, servable); err != nil {
		return err
	}

	// 计数上限:加入新 servable 前,若已达上限就踢全局 LRU。
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

	// deadline 是整个加载流程(含多次 OOM 重试)的总预算,不是每轮 5 分钟;
	// 故意有界,避免 N 次重试长期占住 loadMu 饿死空闲回收 goroutine。
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
			// 多半显存不足:踢同设备最久未用的(排除新 servable 自身),重写 config 重试。
			victim, found := selectLRU(a.loaded, device, servable)
			if !found {
				delete(a.loaded, servable)
				if rerr := a.writeConfigLocked(); rerr != nil {
					logger.Error("openvino: rollback config write failed", zap.String("servable", servable), zap.Error(rerr))
				}
				return fmt.Errorf("openvino servable %q failed to load (no same-device model to evict)", servable)
			}
			delete(a.loaded, victim)
			// 回到循环顶:写入去掉 victim 的 config 并重新等待。
		default: // 超时
			delete(a.loaded, servable)
			if rerr := a.writeConfigLocked(); rerr != nil {
				logger.Error("openvino: rollback config write failed", zap.String("servable", servable), zap.Error(rerr))
			}
			return fmt.Errorf("openvino servable %q load timed out", servable)
		}
	}
}

// waitState 轮询 OVMS 直到 servable 变 AVAILABLE 或 LOADING_FAILED 或到 deadline。
// "END"/""/"LOADING" 视为过渡态继续等。返回终态字符串;超时返回 ""。
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
// enable_prefix_caching + dynamic_split_fuse + cache_size 是性能关键:agent 模式
// 每次会话都注入相同的大段 system prompt(工具+skills 定义),工具调用还会触发多轮
// LLM 请求。没有前缀缓存时每轮都要把几千 token 的前缀从头 prefill(实测 ~3s/轮);
// 开启后重复前缀的 prefill 降到 ~0.2s(实测 15× 提升),跨请求也命中。cache_size
// 单位 GB,2GB 在 19GB 权重 + 24GB 显存的 B60 上留有余量。
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

// loadedModel 记录一个当前驻留(写进 config.json)的 servable 及其设备与最近使用时间。
type loadedModel struct {
	servable string
	device   string // 用于"同设备 LRU"回收,如 "GPU.1"
	lastUsed time.Time
}

// selectLRU 在 loaded 中选 lastUsed 最早(最久未用)的 servable。
// device=="" 表示不限设备;否则只在该设备上选。except 为要排除的 servable 名(""=不排除)。
// 没有候选时返回 ("", false)。纯函数,不触 OVMS / 文件。
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

// selectExpired 返回 loaded 中所有空闲已达 ttl 的 servable 名(now-lastUsed >= ttl),
// 按名字升序以保证确定性。ttl<=0 表示"永不过期",返回 nil。纯函数。
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

// writeConfigLocked 把整个 a.loaded 集(按 servable 名升序)序列化进 config.json。
// OVMS 的 config poller 随后加载新增、卸载移除的 servable。调用方必须已持 a.loadMu。
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

// reconcileFromConfig 在进程启动时从现有 config.json 重建 a.loaded:config.json 可能
// 还列着上次运行驻留的模型(OVMS 也可能仍驻着),内存里却是空的。逐条从其 graph.pbtxt
// 解析 device 填回(lastUsed=now,给完整一个 TTL 周期)。best-effort:任何读/解析失败的
// 条目跳过。无需加锁(仅构造时单线程调用)。
func (a *OpenVINOAdapter) reconcileFromConfig() {
	b, err := os.ReadFile(a.configPath)
	if err != nil {
		return // 还没有 config.json → 没有驻留模型
	}
	var cfg ovmsConfig
	if json.Unmarshal(b, &cfg) != nil {
		return
	}
	now := time.Now()
	for _, e := range cfg.MediapipeConfigList {
		dev := a.parseDeviceFromGraph(e.Name)
		if dev == "" {
			continue // 无法恢复设备 → 跳过
		}
		a.loaded[e.Name] = &loadedModel{servable: e.Name, device: dev, lastUsed: now}
	}
}

// reapOnce 扫一遍 loaded,卸载所有空闲已达 idleTTL 的 servable(对齐 Ollama keep_alive)。
// idleTTL<=0 表示永不卸载,直接返回。有移除才重写 config。持 loadMu。
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

// reaperLoop 每分钟触发一次 reapOnce,长驻后台 goroutine(构造时启动)。
func (a *OpenVINOAdapter) reaperLoop() {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for range ticker.C {
		a.reapOnce()
	}
}

// parseDeviceFromGraph 从 repoPath/<servable>/graph.pbtxt 解析出 device(本加载器自己写的,
// 格式固定含 `device: "GPU.x"`)。读不到 / 解析不出返回 ""。
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
