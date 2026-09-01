package route

import (
	"crypto/ecdsa"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/NimoTech/NimoOS-AI/common"
	"github.com/NimoTech/NimoOS-AI/pkg/config"
	v2 "github.com/NimoTech/NimoOS-AI/route/v2"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/NimoTech/NimoOS-Common/external"
	middleware "github.com/NimoTech/NimoOS-Common/middleware"
	"github.com/NimoTech/NimoOS-Common/utils/jwt"
	"github.com/labstack/echo/v4"
	echo_middleware "github.com/labstack/echo/v4/middleware"
)

func InitV2Router(svc service.Services, runtimePath string, agentURL string, ollamaURL string, openvinoURL string) http.Handler {
	chat := v2.NewChatHandler(svc)
	providers := v2.NewProvidersHandler(svc)
	policy := v2.NewPolicyHandler(svc)
	models := v2.NewModelsHandler(svc, config.Cfg.DataPath+"/models")
	sessions := v2.NewSessionsHandler(svc)
	mcpTickets := v2.NewTicketStore(30 * time.Second)
	runTokens := v2.NewRunTokenStore(24 * time.Hour)
	mcp := v2.NewMCPHandler(svc, mcpTickets, runTokens, agentURL)
	// Task 22: backfill identity cards for MCP servers that existed before
	// this progressive-disclosure feature shipped. Belt-and-braces on top of
	// the TTL self-check in mcp.go's Runtime handler (see
	// StartMigrationBackfill's doc comment) — runs once, in the background,
	// and never blocks startup.
	mcp.StartMigrationBackfill()
	agent := v2.NewAgentHandler(svc, agentURL, 60, mcpTickets)
	agent.StartHealthMonitor()
	parserClient := service.NewParserClient(runtimePath + "/parser.url")
	searchClient := service.NewSearchClient(runtimePath + "/search.url")
	services := v2.NewServicesStatusHandler(agent, ollamaURL, openvinoURL, parserClient, searchClient)
	skills := v2.NewSkillsHandlerFull(svc, agentURL)

	e := echo.New()
	e.Use(echo_middleware.CORSWithConfig(echo_middleware.CORSConfig{
		AllowOrigins: []string{"*"},
		AllowMethods: []string{echo.POST, echo.GET, echo.PUT, echo.DELETE, echo.PATCH, echo.OPTIONS},
		AllowHeaders: []string{
			echo.HeaderAuthorization, echo.HeaderContentType,
			"X-NimoOS-Force-Cloud", "X-User-Id", "X-User-Name",
			"X-Agent-Provider-Key", "X-Agent-Provider-Url",
			"X-Agent-Provider-Type", "X-Agent-Provider-Id",
			"X-Agent-MCP-Ticket",
		},
	}))

	e.Use(echo_middleware.JWTWithConfig(echo_middleware.JWTConfig{
		Skipper: func(c echo.Context) bool {
			p := c.Path()
			if strings.HasPrefix(p, common.V2APIPath+"/_internal/") {
				return true
			}
			if p == common.V2APIPath+"/version" {
				return true
			}
			// The task webhook: credential is the task's own token, checked in
			// Python. Keyed on the MATCHED route pattern, so an encoded or
			// traversed spelling routes to /agent/* instead and still needs a
			// JWT — the skip cannot be reached by spelling the URL differently.
			if v2.IsWebhookTriggerPattern(p) {
				return true
			}
			return v2.MCPDataPath(p) // /v1/ai/mcp[, /*] — token-authed in Python
		},
		ParseTokenFunc: func(token string, c echo.Context) (interface{}, error) {
			valid, claims, err := jwt.Validate(token, func() (*ecdsa.PublicKey, error) {
				return external.GetPublicKey(runtimePath)
			})
			if err != nil || !valid {
				return nil, echo.ErrUnauthorized
			}
			c.Request().Header.Set("X-NimoOS-User-ID", strconv.Itoa(claims.ID))
			c.Request().Header.Set("X-NimoOS-User-Name", claims.Username)
			return claims, nil
		},
		TokenLookupFuncs: []echo_middleware.ValuesExtractor{
			func(c echo.Context) ([]string, error) {
				if auth := c.Request().Header.Get(echo.HeaderAuthorization); auth != "" {
					return []string{strings.TrimPrefix(auth, "Bearer ")}, nil
				}
				// Browsers can't attach Authorization to <img src> or
				// <a href target="_blank"> requests, so attachment raw
				// downloads need a query-string fallback. The token is the
				// same short-lived user JWT; on a single-user home NAS the
				// leak surface is the user's own access logs.
				if tok := c.QueryParam("token"); tok != "" {
					return []string{tok}, nil
				}
				return nil, echo.ErrUnauthorized
			},
		},
	}))

	// Admin enforcement on the DECODED path, after the JWT middleware so an
	// unauthenticated caller still gets 401 rather than 403. Echo routes on the
	// ENCODED path (url.RawPath) while the agent proxy forwards the decoded
	// one, so `/v1/ai/agent/ta%73ks` missed every AdminOnly route below, fell
	// into the /agent/* wildcard and was served by the agent as /agent/tasks.
	// This guard does not depend on which route matched.
	e.Use(v2.AdminPathGuard(runtimePath, common.V2APIPath))

	g := e.Group(common.V2APIPath)

	middleware.RegisterVersionRoute(e, common.V2APIPath+"/version", "AI", common.AIVersion)

	// Internal endpoints: localhost-only, no JWT (e.g. wiki-summary worker)
	internal := g.Group("/_internal", LocalhostOnly)
	internal.POST("/chat/completions", chat.ChatCompletions)
	internal.GET("/models", models.ListInternal)
	internal.GET("/mcp/runtime", mcp.Runtime)
	internal.POST("/mcp/parse", mcp.ParseInternal)
	internal.POST("/mcp/register", mcp.RegisterInternal)
	internal.GET("/mcp/list", mcp.ListInternal)
	internal.POST("/mcp/remove", mcp.RemoveInternal)
	internal.POST("/mcp/approvals", mcp.ApprovalsInternal)
	internal.GET("/mcp/servers/:id/schemas", mcp.SchemasInternal)
	internal.POST("/mcp/token/release", mcp.ReleaseTokenInternal)
	internal.GET("/agent/provider-credentials", v2.ProviderCredentials(svc, runtimePath))
	// user_id here comes from the request body, not a JWT, so LocalhostOnly
	// alone isn't enough (see ValidInternalToken) — require the shared
	// internal token too, same as provider-credentials.
	internal.POST("/skills/install", skills.InstallInternal, v2.InternalTokenOnly(runtimePath))
	internal.POST("/skills/remove", skills.RemoveInternal, v2.InternalTokenOnly(runtimePath))

	// LLM inference endpoints
	g.POST("/chat/completions", chat.ChatCompletions)
	g.POST("/messages", chat.Messages)

	// Provider management
	g.GET("/providers", providers.List)
	g.POST("/providers", providers.Create)
	g.PUT("/providers/:id", providers.Update)
	g.DELETE("/providers/:id", providers.Delete)
	g.GET("/providers/:id/models", providers.ListModels)
	g.POST("/providers/:id/models/refresh", providers.RefreshModels)
	g.PUT("/providers/:id/models", providers.UpdateModels)

	// MCP server management
	g.GET("/mcp/servers", mcp.List)
	g.POST("/mcp/servers", mcp.Create)
	g.POST("/mcp/servers/parse", mcp.Parse)
	g.PUT("/mcp/servers/:id", mcp.Update)
	g.DELETE("/mcp/servers/:id", mcp.Delete)
	g.POST("/mcp/servers/:id/test", mcp.Test)

	// MCP tool approvals (design doc §8.1) — the settings UI's window into
	// the approval store Task 10 built. Public/browser-facing, authenticated
	// like the routes above; see route/v2/mcp_approvals.go.
	g.GET("/mcp/servers/:id/tools", mcp.Tools)
	g.PUT("/mcp/servers/:id/approvals/:tool", mcp.PutApproval)
	g.POST("/mcp/servers/:id/approvals/:tool/ack-description", mcp.AckDescription)
	g.DELETE("/mcp/servers/:id/approvals", mcp.DeleteApprovals)
	g.GET("/mcp/approvals", mcp.ListApprovals)

	// Privacy policy
	g.GET("/policy", policy.Get)
	g.PUT("/policy", policy.Update)

	// Model management
	g.GET("/models", models.List)
	g.POST("/models/pull", models.Pull)
	g.GET("/models/hf/search", models.SearchHF)
	g.GET("/models/hf/files", models.ListHFFiles)
	g.POST("/models/hf/import", models.ImportHF)
	g.GET("/models/hf/import/status", models.ImportStatus)
	g.DELETE("/models/hf/import/cancel", models.CancelImport)
	g.DELETE("/models/:name", models.Delete)

	// Chat session persistence
	g.GET("/sessions", sessions.List)
	g.POST("/sessions", sessions.Create)
	g.DELETE("/sessions/:id", sessions.Delete)
	g.GET("/sessions/:id/messages", sessions.ListMessages)
	g.POST("/sessions/:id/messages", sessions.AppendMessages)
	g.PATCH("/sessions/:id/title", sessions.UpdateTitle)

	// Services status (ollama + agent)
	g.GET("/services/status", services.Status)

	// Parser proxy
	parserProxy := &v2.ParserProxy{Client: parserClient}
	g.GET("/parser/stats", parserProxy.Stats)
	g.GET("/parser/jobs", parserProxy.Jobs)
	g.GET("/parser/folders", parserProxy.Folders)
	g.GET("/parser/state", parserProxy.State)
	g.POST("/parser/control", parserProxy.Control)
	g.POST("/parser/test/analyze", parserProxy.TestAnalyze)
	g.POST("/parser/jobs/retry", parserProxy.RetryJobs)
	g.DELETE("/parser/jobs/:id", parserProxy.DeleteJob)
	g.POST("/parser/jobs/clear-failed", parserProxy.ClearFailedJobs)
	g.GET("/parser/allowlist/extensions", parserProxy.GetAllowlistExtensions)
	g.PATCH("/parser/allowlist/extensions", parserProxy.PatchAllowlistExtension)
	g.GET("/parser/allowlist/folders", parserProxy.GetAllowlistFolders)
	g.POST("/parser/allowlist/folders", parserProxy.PostAllowlistFolder)
	g.DELETE("/parser/allowlist/folders/:id", parserProxy.DeleteAllowlistFolder)
	g.GET("/parser/files", parserProxy.ListFiles)
	g.POST("/parser/files/reindex", parserProxy.ReindexFiles)
	g.GET("/parser/ocr/models", parserProxy.OcrModels)
	g.POST("/parser/ocr/models/:id/install", parserProxy.InstallOcrModel)
	g.POST("/parser/ocr/models/:id/activate", parserProxy.ActivateOcrModel)
	g.DELETE("/parser/ocr/models/:id", parserProxy.DeleteOcrModel)

	// Search proxy → forwards /v1/ai/search/* to the Search service (/v1/search/*)
	searchProxy := &v2.SearchProxy{Client: searchClient}
	g.Any("/search/*", searchProxy.Proxy)

	// MCP Streamable-HTTP proxy (data + token management)
	mcpProxy := v2.NewMCPProxy(agentURL) // same agentURL NewAgentHandler uses
	g.Any("/mcp-rpc", mcpProxy.Serve)
	g.Any("/mcp-rpc/*", mcpProxy.Serve)
	g.Any("/mcp-tokens", mcpProxy.Serve)
	g.Any("/mcp-tokens/*", mcpProxy.Serve)

	// Agent proxy
	g.GET("/agent/health", agent.Health)
	// Admin-scoped agent endpoints, registered (with their /* subtrees) BEFORE
	// the general /agent/* wildcard below so the static segment wins. The list
	// itself lives in v2.AdminScopedAgentPaths, which is also what the
	// decoded-path guard installed above enforces — one list, both layers, so
	// a new admin endpoint cannot be gated in one place and open in the other.
	for _, p := range v2.AdminScopedAgentPaths {
		g.Any(p, agent.Proxy, v2.AdminOnly(runtimePath))
		g.Any(p+"/*", agent.Proxy, v2.AdminOnly(runtimePath))
	}
	// The task webhook trigger (M3). Registered ahead of the wildcard as its
	// own POST route so echo's matched pattern — which is what the JWT skipper
	// keys off — identifies it exactly. Unauthenticated by design: the task's
	// webhook_token is the whole credential, and ProxyAnonymous strips every
	// identity header so a caller cannot name the user whose task runs.
	g.POST(v2.WebhookTriggerRoute, agent.ProxyAnonymous)
	g.Any("/agent/*", func(c echo.Context) error {
		return agent.Proxy(c)
	})

	// Filesystem mounts (picker scope)
	fs := v2.NewFSHandler()
	g.GET("/fs/mounts", fs.Mounts)

	// Hard blacklist CRUD
	blacklist := v2.NewBlacklistHandler(svc)
	g.GET("/blacklist", blacklist.List)
	g.POST("/blacklist", blacklist.Create)
	g.DELETE("/blacklist/:id", blacklist.Delete)

	// Skill management
	g.GET("/skills", skills.List)
	g.POST("/skills", skills.Create)
	g.GET("/skills/:id", skills.Get)
	g.PATCH("/skills/:id", skills.Update)
	g.DELETE("/skills/:id", skills.Delete)
	g.POST("/skills/:id/test", skills.TestStream)
	g.GET("/skills/:id/files/*", skills.GetFile)
	g.GET("/skills/:id/export", skills.ExportTarGz)

	return e
}
