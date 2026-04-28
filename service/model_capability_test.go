package service

import "testing"

func TestSupportsThinking(t *testing.T) {
	cases := []struct {
		providerType string
		modelName    string
		want         bool
	}{
		{"deepseek", "deepseek-v4-pro", true},
		{"deepseek", "deepseek-reasoner", true},
		{"deepseek", "anything", true},
		{"anthropic", "claude-3-7-sonnet-20250219", true},
		{"anthropic", "claude-4-sonnet", true},
		{"anthropic", "claude-3-5-sonnet-20241022", false},
		{"anthropic", "claude-3-opus", false},
		{"openai", "o1-preview", true},
		{"openai", "o3-mini", true},
		{"openai", "o4-mini", true},
		{"openai", "gpt-5", true},
		{"openai", "gpt-5-turbo", true},
		{"openai", "gpt-4o", false},
		{"openai", "gpt-4-turbo", false},
		{"qwen", "qwen3-72b", false},
		{"ollama", "llama3", false},
		{"other", "anything", false},
		{"", "", false},
	}
	for _, c := range cases {
		got := SupportsThinking(c.providerType, c.modelName)
		if got != c.want {
			t.Errorf("SupportsThinking(%q,%q)=%v want %v",
				c.providerType, c.modelName, got, c.want)
		}
	}
}
