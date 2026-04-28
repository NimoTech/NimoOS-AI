package service

import "testing"

func TestClassifyByBaseURL(t *testing.T) {
	cases := []struct {
		baseURL  string
		protocol Protocol
		want     string
	}{
		{"https://api.deepseek.com", ProtocolOpenAI, "deepseek"},
		{"https://api.deepseek.com/v1", ProtocolOpenAI, "deepseek"},
		{"https://api.openai.com/v1", ProtocolOpenAI, "openai"},
		{"https://dashscope.aliyuncs.com/compatible-mode/v1", ProtocolOpenAI, "qwen"},
		{"https://api.anthropic.com/v1", ProtocolAnthropic, "anthropic"},
		{"http://127.0.0.1:11434/v1", ProtocolOpenAI, "ollama"},
		{"https://my-llm.example.com", ProtocolOpenAI, "other"},
	}
	for _, c := range cases {
		got := ClassifyProvider(c.baseURL, c.protocol)
		if got != c.want {
			t.Errorf("ClassifyProvider(%q,%q) = %q, want %q",
				c.baseURL, c.protocol, got, c.want)
		}
	}
}
