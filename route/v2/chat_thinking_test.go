package v2

import (
	"encoding/json"
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/stretchr/testify/require"
)

func TestExtractThinkingControl(t *testing.T) {
	tests := []struct {
		name     string
		body     []byte
		expected service.ThinkingControl
	}{
		{
			name:     "empty body",
			body:     []byte("{}"),
			expected: service.ThinkingControl{Enabled: false, Level: ""},
		},
		{
			name:     "reasoning_effort minimal",
			body:     []byte(`{"reasoning_effort":"minimal"}`),
			expected: service.ThinkingControl{Enabled: false, Level: "medium"},
		},
		{
			name:     "reasoning_effort low",
			body:     []byte(`{"reasoning_effort":"low"}`),
			expected: service.ThinkingControl{Enabled: true, Level: "low"},
		},
		{
			name:     "reasoning_effort medium",
			body:     []byte(`{"reasoning_effort":"medium"}`),
			expected: service.ThinkingControl{Enabled: true, Level: "medium"},
		},
		{
			name:     "reasoning_effort high",
			body:     []byte(`{"reasoning_effort":"high"}`),
			expected: service.ThinkingControl{Enabled: true, Level: "high"},
		},
		{
			name:     "reasoning_effort max",
			body:     []byte(`{"reasoning_effort":"max"}`),
			expected: service.ThinkingControl{Enabled: true, Level: "max"},
		},
		{
			name:     "reasoning_effort unknown_value defaults to medium",
			body:     []byte(`{"reasoning_effort":"unknown_value"}`),
			expected: service.ThinkingControl{Enabled: true, Level: "medium"},
		},
		{
			name:     "extra_body thinking type disabled",
			body:     []byte(`{"extra_body":{"thinking":{"type":"disabled"}}}`),
			expected: service.ThinkingControl{Enabled: false, Level: ""},
		},
		{
			name:     "extra_body thinking type enabled",
			body:     []byte(`{"extra_body":{"thinking":{"type":"enabled"}}}`),
			expected: service.ThinkingControl{Enabled: true, Level: ""},
		},
		{
			name:     "reasoning_effort high overridden by extra_body disabled",
			body:     []byte(`{"reasoning_effort":"high","extra_body":{"thinking":{"type":"disabled"}}}`),
			expected: service.ThinkingControl{Enabled: false, Level: "high"},
		},
		{
			name:     "reasoning_effort minimal overridden by extra_body enabled",
			body:     []byte(`{"reasoning_effort":"minimal","extra_body":{"thinking":{"type":"enabled"}}}`),
			expected: service.ThinkingControl{Enabled: true, Level: "medium"},
		},
		{
			name:     "malformed JSON returns zero value",
			body:     []byte("{not json}"),
			expected: service.ThinkingControl{Enabled: false, Level: ""},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			result := extractThinkingControl(tc.body)
			require.Equal(t, tc.expected.Enabled, result.Enabled, "Enabled mismatch")
			require.Equal(t, tc.expected.Level, result.Level, "Level mismatch")
		})
	}
}

func TestMapEffortToLevel(t *testing.T) {
	tests := []struct {
		input    string
		expected string
	}{
		{input: "minimal", expected: "medium"},
		{input: "low", expected: "low"},
		{input: "medium", expected: "medium"},
		{input: "high", expected: "high"},
		{input: "max", expected: "max"},
		{input: "", expected: "medium"},
		{input: "garbage", expected: "medium"},
	}

	for _, tc := range tests {
		t.Run(tc.input, func(t *testing.T) {
			result := mapEffortToLevel(tc.input)
			require.Equal(t, tc.expected, result)
		})
	}
}

// TestExtractThinkingControl_ComplexCases tests additional edge cases with nested JSON.
func TestExtractThinkingControl_ComplexCases(t *testing.T) {
	tests := []struct {
		name     string
		body     []byte
		expected service.ThinkingControl
	}{
		{
			name:     "both reasoning_effort and extra_body, extra_body wins",
			body:     []byte(`{"reasoning_effort":"medium","extra_body":{"thinking":{"type":"disabled"}}}`),
			expected: service.ThinkingControl{Enabled: false, Level: "medium"},
		},
		{
			name:     "extra_body thinking with missing type field defaults to no override",
			body:     []byte(`{"reasoning_effort":"high","extra_body":{"thinking":{}}}`),
			expected: service.ThinkingControl{Enabled: true, Level: "high"},
		},
		{
			name:     "extra_body thinking with unknown type value defaults to no override",
			body:     []byte(`{"reasoning_effort":"low","extra_body":{"thinking":{"type":"unknown"}}}`),
			expected: service.ThinkingControl{Enabled: true, Level: "low"},
		},
		{
			name:     "malformed reasoning_effort doesn't crash",
			body:     []byte(`{"reasoning_effort":123}`),
			expected: service.ThinkingControl{Enabled: false, Level: ""},
		},
		{
			name:     "malformed extra_body doesn't crash",
			body:     []byte(`{"reasoning_effort":"high","extra_body":"not_a_map"}`),
			expected: service.ThinkingControl{Enabled: true, Level: "high"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			result := extractThinkingControl(tc.body)
			require.Equal(t, tc.expected.Enabled, result.Enabled, "Enabled mismatch")
			require.Equal(t, tc.expected.Level, result.Level, "Level mismatch")
		})
	}
}

// TestExtractThinkingControl_JSONRoundTrip ensures the function handles realistic JSON structures.
func TestExtractThinkingControl_JSONRoundTrip(t *testing.T) {
	type openAIRequest struct {
		Model             string         `json:"model"`
		ReasoningEffort   string         `json:"reasoning_effort,omitempty"`
		ExtraBody         json.RawMessage `json:"extra_body,omitempty"`
	}

	req := openAIRequest{
		Model:           "gpt-4",
		ReasoningEffort: "high",
	}
	body, err := json.Marshal(req)
	require.NoError(t, err)

	result := extractThinkingControl(body)
	require.True(t, result.Enabled)
	require.Equal(t, "high", result.Level)
}
