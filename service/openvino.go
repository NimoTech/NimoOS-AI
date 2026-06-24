package service

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

// ── 纯函数:用户名 ↔ OVMS 内部名映射 ──────────────────────────

// OVMSModelName maps a user-facing (model, device) pair to the OVMS internal
// servable name. "qwen3-vl-int4","GPU.1" → "qwen3-vl-int4-gpu1".
// OVMS model names cannot contain '@' or '.', so the device is lowercased and
// its dot stripped, then joined with '-'.
func OVMSModelName(model, device string) string {
	d := strings.ToLower(strings.ReplaceAll(device, ".", ""))
	return model + "-" + d
}

// ovmsDisplayName reverses OVMSModelName for the model list: it finds a trailing
// "-gpuN" / "-npuN" / "-cpu" segment and renders it as "@GPU.N" etc.
// If no known device suffix is found, the internal name is returned unchanged.
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

// OpenVINOAdapter proxies LLM requests to a local OVMS instance and knows which
// devices are resident (from config).
type OpenVINOAdapter struct {
	baseURL string
	devices []string // resident devices, e.g. ["GPU.1"]; devices[0] is the default
	client  *http.Client
}

// NewOpenVINOAdapter builds an adapter. devicesCSV is the comma-separated
// resident device list (config OpenVINODevices), e.g. "GPU.1" or "GPU.1,GPU.0".
func NewOpenVINOAdapter(baseURL, devicesCSV string) *OpenVINOAdapter {
	var devs []string
	for _, d := range strings.Split(devicesCSV, ",") {
		if t := strings.TrimSpace(d); t != "" {
			devs = append(devs, t)
		}
	}
	if len(devs) == 0 {
		devs = []string{"GPU.1"}
	}
	return &OpenVINOAdapter{
		baseURL: baseURL,
		devices: devs,
		client:  &http.Client{}, // no timeout: streaming can be long
	}
}

// Devices returns the resident device list.
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
