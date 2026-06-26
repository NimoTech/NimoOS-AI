package service

import "strings"

// ClassifyProvider returns one of:
//
//	"deepseek" / "openai" / "anthropic" / "qwen" / "ollama" / "other"
//
// based on a heuristic over (baseURL, protocol). Used for a one-time
// migration to backfill the provider_type column and as a fallback when
// the user hasn't explicitly chosen a type.
func ClassifyProvider(baseURL string, protocol Protocol) string {
	if protocol == ProtocolAnthropic {
		return "anthropic"
	}
	host := strings.ToLower(baseURL)
	switch {
	case strings.Contains(host, "api.deepseek.com"):
		return "deepseek"
	case strings.Contains(host, "api.openai.com"):
		return "openai"
	case strings.Contains(host, "dashscope") || strings.Contains(host, "aliyuncs.com"):
		return "qwen"
	case strings.Contains(host, "127.0.0.1:11434") || strings.Contains(host, "localhost:11434"):
		return "ollama"
	case strings.Contains(host, "127.0.0.1:9100") || strings.Contains(host, "localhost:9100"):
		return "openvino"
	default:
		return "other"
	}
}
