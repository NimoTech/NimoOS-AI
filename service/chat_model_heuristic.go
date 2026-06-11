package service

import "regexp"

// LooksLikeChatModel is a heuristic used ONLY to order/group the settings page
// (chat models first). It never hides or blocks any model — non-matches are
// still selectable and routable. Errs toward inclusion.
func LooksLikeChatModel(providerType, name string) bool {
	if nonChatRe.MatchString(name) {
		return false
	}
	switch providerType {
	case "openai":
		return openaiChatRe.MatchString(name)
	case "anthropic":
		return anthropicChatRe.MatchString(name)
	case "deepseek":
		return true
	default:
		// qwen/ollama/other: keep everything that isn't obviously non-chat.
		return true
	}
}

var (
	// Obvious non-conversational families across providers.
	nonChatRe      = regexp.MustCompile(`(?i)embed|whisper|tts|audio|dall-?e|image|moderation|rerank|vision-encoder`)
	openaiChatRe   = regexp.MustCompile(`(?i)^(gpt-|o1|o3|o4|chatgpt)`)
	anthropicChatRe = regexp.MustCompile(`(?i)^claude`)
)
