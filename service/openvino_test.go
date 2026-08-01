package service

import "testing"

func TestOVMSModelName(t *testing.T) {
	cases := []struct{ model, device, want string }{
		{"qwen3-vl-int4", "GPU.1", "qwen3-vl-int4-gpu1"},
		{"qwen3-vl-int4", "GPU.0", "qwen3-vl-int4-gpu0"},
		{"llama3", "CPU", "llama3-cpu"},
		// dir names with dots must be sanitized into a valid servable name (dot→hyphen)
		{"qwen3.6-35b-a3b-int4", "GPU.1", "qwen3-6-35b-a3b-int4-gpu1"},
		{"qwen2-0.5b-int4", "GPU.0", "qwen2-0-5b-int4-gpu0"},
	}
	for _, c := range cases {
		if got := OVMSModelName(c.model, c.device); got != c.want {
			t.Errorf("OVMSModelName(%q,%q)=%q want %q", c.model, c.device, got, c.want)
		}
	}
}

func TestOVMSDisplayName(t *testing.T) {
	cases := []struct{ internal, want string }{
		{"qwen3-vl-int4-gpu1", "qwen3-vl-int4@GPU.1"},
		{"qwen3-vl-int4-gpu0", "qwen3-vl-int4@GPU.0"},
		{"llama3-cpu", "llama3@CPU"},
		{"no-device-suffix", "no-device-suffix"}, // returned as-is when the suffix is not recognized
	}
	for _, c := range cases {
		if got := ovmsDisplayName(c.internal); got != c.want {
			t.Errorf("ovmsDisplayName(%q)=%q want %q", c.internal, got, c.want)
		}
	}
}
