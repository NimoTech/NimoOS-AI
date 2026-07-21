package service

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ── 纯函数:用户名 ↔ OVMS 内部名映射 ──────────────────────────

// OVMSModelName maps a user-facing (model, device) pair to the OVMS internal
// servable name. "qwen3-vl-int4","GPU.1" → "qwen3-vl-int4-gpu1";
// "qwen3.6-35b-a3b-int4","GPU.1" → "qwen3-6-35b-a3b-int4-gpu1".
// OVMS servable names cannot contain '@' or '.', so both the model name and the
// device are sanitized: lowercased with every char outside [a-z0-9-] replaced by
// '-'. The model's original (dotted) name stays the user-facing display name; the
// sanitized form is only the internal/servable + repo directory name.
func OVMSModelName(model, device string) string {
	// device: lowercase, dots stripped → GPU.1→gpu1 (keeps the legacy form).
	dev := strings.ToLower(strings.ReplaceAll(device, ".", ""))
	return sanitizeServablePart(model) + "-" + dev
}

// sanitizeServablePart lowercases s and replaces any char outside [a-z0-9-] with
// '-' so the result is a legal OVMS servable-name component.
func sanitizeServablePart(s string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(s) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-' {
			b.WriteRune(r)
		} else {
			b.WriteRune('-')
		}
	}
	return b.String()
}

// ovmsDisplayName reverses OVMSModelName for the model list: it finds a trailing
// "-gpuN" / "-npuN" / "-cpu" segment and renders it as "@GPU.N" etc.
// If no known device suffix is found, the internal name is returned unchanged.
//
// Precondition: an OVMS servable name must end in the format produced by
// OVMSModelName (i.e. -gpuN / -npuN / -cpu). If the OVMS config uses any other
// naming scheme, this function cannot recover the device part and the model list
// will show the raw internal name.
func ovmsDisplayName(internal string) string {
	idx := strings.LastIndex(internal, "-")
	if idx < 0 || idx == len(internal)-1 {
		return internal
	}
	model, suffix := internal[:idx], internal[idx+1:]
	low := strings.ToLower(suffix)
	switch {
	case strings.HasPrefix(low, "gpu"):
		return model + "@GPU." + suffix[3:]
	case strings.HasPrefix(low, "npu"):
		return model + "@NPU." + suffix[3:]
	case low == "cpu":
		return model + "@CPU"
	}
	return internal
}

// ── OpenVINOChecker:健康监控(对称 OllamaChecker) ───────────

// OpenVINOChecker polls OVMS readiness and fires callbacks on state changes.
// OVMS is managed by systemd; this checker only monitors.
type OpenVINOChecker struct {
	baseURL     string
	client      *http.Client
	failures    int32
	maxFailures int32
	alertSent   atomic.Bool
	onUnhealthy func()
	onRecovered func()
}

func NewOpenVINOChecker(baseURL string) *OpenVINOChecker {
	return &OpenVINOChecker{
		baseURL:     baseURL,
		client:      &http.Client{Timeout: 3 * time.Second},
		maxFailures: 3,
		onUnhealthy: func() {},
		onRecovered: func() {},
	}
}

func (o *OpenVINOChecker) SetCallbacks(onUnhealthy, onRecovered func()) {
	o.onUnhealthy = onUnhealthy
	o.onRecovered = onRecovered
}

// IsHealthy returns true if OVMS responds 200 to GET /v2/health/ready.
func (o *OpenVINOChecker) IsHealthy() bool {
	resp, err := o.client.Get(o.baseURL + "/v2/health/ready")
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func (o *OpenVINOChecker) check() {
	if o.IsHealthy() {
		if o.alertSent.Swap(false) {
			o.onRecovered()
		}
		atomic.StoreInt32(&o.failures, 0)
		return
	}
	count := atomic.AddInt32(&o.failures, 1)
	if count >= o.maxFailures && !o.alertSent.Swap(true) {
		o.onUnhealthy()
	}
}

func (o *OpenVINOChecker) Start(ctx context.Context) {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			o.check()
		}
	}
}

// ── OpenVINOAdapter:代理 + 设备集 + 列模型 ──────────────────

// OpenVINOAdapter proxies LLM requests to a local OVMS instance. Models are NOT
// pre-loaded: they live as IR dirs under srcModelsPath and are listed as options;
// a model is loaded into OVMS on first use (EnsureLoaded), Ollama-style.
type OpenVINOAdapter struct {
	baseURL string
	devices []string // selectable devices, e.g. ["GPU.1"]; devices[0] is the default

	// On-demand model loading paths.
	srcModelsPath string // raw IR model dirs the user drops in (one subdir per model)
	repoPath      string // OVMS servable repo (staged graph.pbtxt + version dirs)
	configPath    string // OVMS config.json this adapter rewrites to load/unload
	loadMu        sync.Mutex
	client        *http.Client

	// 多模型驻留 + 空闲回收(对齐 Ollama)。loaded 是写入 config.json 的常驻集,唯一事实源;
	// 所有读写都在 loadMu 下。
	loaded      map[string]*loadedModel
	maxLoaded   int           // 同时最多驻留数,默认 3
	idleTTL     time.Duration // 空闲多久卸载;0=永不卸载
	cacheSizeGB int           // 每个 servable 的 KV cache 池大小(GB),默认 2
}

const (
	defaultOVSrcModelsPath = "/var/lib/nimoos/ai/models"
	defaultOVRepoPath      = "/var/lib/nimoos/ai/openvino/models"
	defaultOVConfigPath    = "/var/lib/nimoos/ai/openvino/config.json"
)

// NewOpenVINOAdapter builds an adapter. devicesCSV is the comma-separated
// selectable device list (config OpenVINODevices), e.g. "GPU.1" or "GPU.1,GPU.0".
// maxLoaded 是同时最多驻留的模型数(<=0 视作默认 3);idleTTLMinutes 是空闲卸载分钟数
// (0 = 永不卸载);cacheSizeGB 是每个 servable 的 KV cache 池大小(GB,<=0 视作默认 2)。
// 构造时按 OVMS 实时服务集重建驻留集(reconcileFromOVMS)。
func NewOpenVINOAdapter(baseURL, devicesCSV string, maxLoaded, idleTTLMinutes, cacheSizeGB int) *OpenVINOAdapter {
	var devs []string
	for _, d := range strings.Split(devicesCSV, ",") {
		if t := strings.TrimSpace(d); t != "" {
			devs = append(devs, t)
		}
	}
	if len(devs) == 0 {
		devs = []string{"GPU.1"}
	}
	if maxLoaded <= 0 {
		maxLoaded = 3
	}
	if cacheSizeGB <= 0 {
		cacheSizeGB = 2
	}
	a := &OpenVINOAdapter{
		baseURL:       baseURL,
		devices:       devs,
		srcModelsPath: defaultOVSrcModelsPath,
		repoPath:      defaultOVRepoPath,
		configPath:    defaultOVConfigPath,
		client:        &http.Client{}, // no timeout: streaming can be long
		loaded:        map[string]*loadedModel{},
		maxLoaded:     maxLoaded,
		idleTTL:       time.Duration(idleTTLMinutes) * time.Minute,
		cacheSizeGB:   cacheSizeGB,
	}
	a.reconcileFromOVMS()
	go a.reaperLoop()
	return a
}

// Devices returns the selectable device list.
func (a *OpenVINOAdapter) Devices() []string { return a.devices }

// DefaultDevice returns the device used when the caller omits "@device".
func (a *OpenVINOAdapter) DefaultDevice() string { return a.devices[0] }

// HasDevice reports whether dev is a configured resident device.
func (a *OpenVINOAdapter) HasDevice(dev string) bool {
	for _, d := range a.devices {
		if d == dev {
			return true
		}
	}
	return false
}

// ChatCompletions forwards the request body to OVMS /v3/chat/completions
// (OpenAI-compatible). The caller must read and close resp.Body.
func (a *OpenVINOAdapter) ChatCompletions(body io.Reader) (*http.Response, error) {
	data, err := io.ReadAll(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequest(http.MethodPost, a.baseURL+"/v3/chat/completions", bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	return a.client.Do(req)
}

// ListServedModels queries OVMS GET /v1/config and returns the internal names of
// servables currently AVAILABLE. Returns nil (not an error) when OVMS is down so
// callers can degrade gracefully.
func (a *OpenVINOAdapter) ListServedModels() []string {
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get(a.baseURL + "/v1/config")
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	// /v1/config shape: { "<name>": { "model_version_status": [ {"state":"AVAILABLE"} ] } }
	var cfg map[string]struct {
		Status []struct {
			State string `json:"state"`
		} `json:"model_version_status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&cfg); err != nil {
		return nil
	}
	var names []string
	for name, st := range cfg {
		for _, s := range st.Status {
			if s.State == "AVAILABLE" {
				names = append(names, name)
				break
			}
		}
	}
	return names
}
