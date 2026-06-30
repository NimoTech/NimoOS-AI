// NimoOS-AI/route/v2/mcp_proxy_test.go
package v2

import "testing"

func TestMCPAuthSkip(t *testing.T) {
	cases := map[string]bool{
		"/v1/ai/mcp":          true,  // data endpoint — token-authed in Python
		"/v1/ai/mcp/*":        true,  // session subpaths
		"/v1/ai/mcp-tokens":   false, // management — must stay JWT-protected
		"/v1/ai/mcp-tokens/*": false,
		"/v1/ai/search/*":     false,
	}
	for path, want := range cases {
		if got := MCPDataPath(path); got != want {
			t.Errorf("MCPDataPath(%q)=%v want %v", path, got, want)
		}
	}
}
