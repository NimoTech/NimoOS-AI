// NimoOS-AI/route/v2/mcp_proxy_test.go
package v2

import "testing"

func TestMCPAuthSkip(t *testing.T) {
	cases := map[string]bool{
		// data endpoint — token-authed in Python (must be JWT-exempt)
		"/v1/ai/mcp-rpc":   true,
		"/v1/ai/mcp-rpc/*": true,

		// management CRUD routes — must stay JWT-protected (regression cases)
		"/v1/ai/mcp/servers":        false,
		"/v1/ai/mcp/servers/:id":    false,
		"/v1/ai/mcp/servers/:id/test": false,

		// token management — must stay JWT-protected
		"/v1/ai/mcp-tokens":   false,
		"/v1/ai/mcp-tokens/*": false,

		// other routes — must stay JWT-protected
		"/v1/ai/search/*": false,
	}
	for path, want := range cases {
		if got := MCPDataPath(path); got != want {
			t.Errorf("MCPDataPath(%q)=%v want %v", path, got, want)
		}
	}
}
