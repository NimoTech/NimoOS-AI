// NimoOS-AI/route/v2/mcp_proxy.go
package v2

import (
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"

	"github.com/NimoTech/NimoOS-AI/common"
	"github.com/labstack/echo/v4"
)

// MCPDataPath reports whether an Echo route path is the JWT-exempt MCP data
// endpoint. Data (/v1/ai/mcp-rpc[, /*]) is token-authed inside the Python agent;
// the management endpoints (/v1/ai/mcp/servers, /v1/ai/mcp-tokens) must NOT be exempt.
func MCPDataPath(p string) bool {
	base := common.V2APIPath + "/mcp-rpc"
	return p == base || strings.HasPrefix(p, base+"/")
}

// MCPProxy reverse-proxies /v1/ai/mcp* to the Python agent, stripping the
// /v1/ai prefix (so /v1/ai/mcp → /mcp, /v1/ai/mcp-tokens → /mcp-tokens).
// The data endpoint carries no user JWT (Python authenticates the Bearer
// token); the management endpoint relies on the JWT middleware having set
// X-NimoOS-User-ID, which the reverse proxy forwards as-is.
type MCPProxy struct{ proxy *httputil.ReverseProxy }

func NewMCPProxy(agentURL string) *MCPProxy {
	target, _ := url.Parse(agentURL)
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.FlushInterval = 100 * time.Millisecond
	orig := proxy.Director
	proxy.Director = func(req *http.Request) {
		orig(req)
		req.URL.Path = strings.TrimPrefix(req.URL.Path, "/v1/ai")
		if req.URL.RawPath != "" {
			req.URL.RawPath = strings.TrimPrefix(req.URL.RawPath, "/v1/ai")
		}
	}
	return &MCPProxy{proxy: proxy}
}

// Serve reverse-proxies the request to the Python agent. One handler covers
// both the data and management paths (the Director rewrites by prefix).
func (m *MCPProxy) Serve(c echo.Context) error {
	m.proxy.ServeHTTP(c.Response(), c.Request())
	return nil
}
