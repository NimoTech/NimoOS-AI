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

// ── pure functions: user-facing name ↔ OVMS internal name mapping ──────────

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

// ── OpenVINOChecker: health monitoring (mirrors OllamaChecker) ─────────────

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

// ── OpenVINOAdapter: proxy + device set + model listing ────────────────────

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

	// Multi-model residency + idle reaping (mirrors Ollama). loaded is the resident
	// set written to config.json, the single source of truth; all reads/writes happen
	// under loadMu.
	loaded    map[string]*loadedModel
	maxLoaded int           // max number resident at once, default 3
	idleTTL   time.Duration // how long idle before unloading; 0 = never unload
}

const (
	defaultOVSrcModelsPath = "/var/lib/nimoos/ai/models"
	defaultOVRepoPath      = "/var/lib/nimoos/ai/openvino/models"
	defaultOVConfigPath    = "/var/lib/nimoos/ai/openvino/config.json"
)

// NewOpenVINOAdapter builds an adapter. devicesCSV is the comma-separated
// selectable device list (config OpenVINODevices), e.g. "GPU.1" or "GPU.1,GPU.0".
// maxLoaded is the max number of models resident at once (<=0 is treated as the
// default 3); idleTTLMinutes is the idle-unload minutes (0 = never unload).
// The resident set is rebuilt from OVMS's live serving set at construction time
// (reconcileFromOVMS).
func NewOpenVINOAdapter(baseURL, devicesCSV string, maxLoaded, idleTTLMinutes int) *OpenVINOAdapter {
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
