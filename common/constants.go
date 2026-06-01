package common

const (
	AIVersion = "1.9.0-alpha1"

	OllamaBaseURL    = "http://127.0.0.1:11434"
	OllamaHealthPath = "/api/tags"

	URLFileName = "ai.url"
	Localhost   = "127.0.0.1"

	// MessageBus event types
	EventOllamaUnhealthy = "AI:OllamaUnhealthy"
	EventOllamaRecovered = "AI:OllamaRecovered"

	// API paths
	V2APIPath = "/v1/ai"
	V2DocPath = "/doc/v1/ai"
)
