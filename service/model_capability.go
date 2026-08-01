package service

import "regexp"

// SupportsThinking reports whether (provider type, model name) supports
// a user-controlled thinking budget. Used by /v1/ai/providers and /v1/ai/models
// to populate the supports_thinking flag the UI uses to enable/disable the
// thinking bar.
//
// Rules:
//
//	deepseek        → always true (all DeepSeek models support thinking mode)
//	anthropic       → claude-3-7-* and claude-4-* and beyond
//	openai          → o-series (o1/o3/o4) and gpt-5+
//	qwen / ollama   → qwen3*, deepseek-r1*, *-think* / *-thinking* tagged variants
//	openvino        → same rules as local (qwen3*/deepseek-r1*/think...)
//	other/empty     → false
func SupportsThinking(providerType, modelName string) bool {
	switch providerType {
	case "deepseek":
		return true
	case "anthropic":
		return claudeThinkingRe.MatchString(modelName)
	case "openai":
		return openaiThinkingRe.MatchString(modelName)
	case "qwen", "ollama":
		return localThinkingRe.MatchString(modelName)
	case "openvino":
		return localThinkingRe.MatchString(modelName)
	}
	return false
}

var (
	claudeThinkingRe = regexp.MustCompile(`^claude-(3-7|4-|5-)`)
	openaiThinkingRe = regexp.MustCompile(`^(o1|o3|o4|gpt-5)`)
	// Matches Qwen3 family (qwen3, qwen3.5, qwen3-coder, ...), DeepSeek-R1 distills,
	// and any tag containing "think" / "thinking" / "reasoning" / "-r1".
	localThinkingRe = regexp.MustCompile(`(?i)^qwen3|^deepseek-r1|think|reasoning|-r1`)
)
