package v2

import "testing"

func TestParseOVMSDeviceSuffix(t *testing.T) {
	cases := []struct {
		in         string
		bare, dev  string
		ok         bool
	}{
		{"qwen3-vl-int4@GPU.1", "qwen3-vl-int4", "GPU.1", true},
		{"qwen3-vl-int4@GPU.0", "qwen3-vl-int4", "GPU.0", true},
		{"llama3@CPU", "llama3", "CPU", true},
		{"model@NPU.0", "model", "NPU.0", true},
		{"llama3", "", "", false},        // ollama: 无 @,非 openvino
		{"deepseek-chat", "", "", false}, // cloud: 无 @
		{"@GPU.1", "", "", false},        // 空 bare
		{"model@", "", "", false},        // 空 device
		{"model@something", "", "", false}, // 非已知设备
	}
	for _, c := range cases {
		bare, dev, ok := parseOVMSDeviceSuffix(c.in)
		if ok != c.ok || bare != c.bare || dev != c.dev {
			t.Errorf("parseOVMSDeviceSuffix(%q) = (%q,%q,%v), want (%q,%q,%v)",
				c.in, bare, dev, ok, c.bare, c.dev, c.ok)
		}
	}
}
