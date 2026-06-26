package common

const (
	AIVersion = "1.9.0-alpha1"

	OllamaBaseURL    = "http://127.0.0.1:11434"
	OllamaHealthPath = "/api/tags"

	OpenVINOBaseURL    = "http://127.0.0.1:9100"
	OpenVINOHealthPath = "/v2/health/ready"

	URLFileName = "ai.url"
	Localhost   = "127.0.0.1"

	// MessageBus event types
	EventOllamaUnhealthy = "AI:OllamaUnhealthy"
	EventOllamaRecovered = "AI:OllamaRecovered"

	EventOpenVINOUnhealthy = "AI:OpenVINOUnhealthy"
	EventOpenVINORecovered = "AI:OpenVINORecovered"

	// API paths
	V2APIPath = "/v1/ai"
	V2DocPath = "/doc/v1/ai"
)
