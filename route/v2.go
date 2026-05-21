package route

import (
	"crypto/ecdsa"
	"net/http"
	"strconv"
	"strings"

	"github.com/NimoTech/NimoOS-AI/common"
	"github.com/NimoTech/NimoOS-AI/pkg/config"
	v2 "github.com/NimoTech/NimoOS-AI/route/v2"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/NimoTech/NimoOS-Common/external"
	"github.com/NimoTech/NimoOS-Common/utils/jwt"
	"github.com/labstack/echo/v4"
	echo_middleware "github.com/labstack/echo/v4/middleware"
)

func InitV2Router(svc service.Services, runtimePath string, agentURL string, ollamaURL string) http.Handler {
	chat := v2.NewChatHandler(svc)
	providers := v2.NewProvidersHandler(svc)
	policy := v2.NewPolicyHandler(svc)
	models := v2.NewModelsHandler(svc, config.Cfg.DataPath+"/models")
	sessions := v2.NewSessionsHandler(svc)
	agent := v2.NewAgentHandler(svc, agentURL, 60)
	agent.StartHealthMonitor()
	services := v2.NewServicesStatusHandler(agent, ollamaURL)

	e := echo.New()
	e.Use(echo_middleware.CORSWithConfig(echo_middleware.CORSConfig{
		AllowOrigins: []string{"*"},
		AllowMethods: []string{echo.POST, echo.GET, echo.PUT, echo.DELETE, echo.PATCH, echo.OPTIONS},
		AllowHeaders: []string{
			echo.HeaderAuthorization, echo.HeaderContentType,
			"X-NimoOS-Force-Cloud", "X-User-Id", "X-User-Name",
			"X-Agent-Provider-Key", "X-Agent-Provider-Url",
			"X-Agent-Provider-Type",
		},
	}))

	e.Use(echo_middleware.JWTWithConfig(echo_middleware.JWTConfig{
		Skipper: func(c echo.Context) bool {
			// _internal routes are localhost-only (LocalhostOnly middleware) and
			// accept X-NimoOS-User-ID from the request directly. They serve local
			// daemon traffic (e.g. wiki-summary worker) that has no JWT.
			return strings.HasPrefix(c.Path(), common.V2APIPath+"/_internal/")
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

	g := e.Group(common.V2APIPath)

	// Internal endpoints: localhost-only, no JWT (e.g. wiki-summary worker)
	internal := g.Group("/_internal", LocalhostOnly)
	internal.POST("/chat/completions", chat.ChatCompletions)

	// LLM inference endpoints
	g.POST("/chat/completions", chat.ChatCompletions)
	g.POST("/messages", chat.Messages)

	// Provider management
	g.GET("/providers", providers.List)
	g.POST("/providers", providers.Create)
	g.PUT("/providers/:id", providers.Update)
	g.DELETE("/providers/:id", providers.Delete)

	// Privacy policy
	g.GET("/policy", policy.Get)
	g.PUT("/policy", policy.Update)

	// Model management
	g.GET("/models", models.List)
	g.POST("/models/pull", models.Pull)
	g.GET("/models/hf/search", models.SearchHF)
	g.GET("/models/hf/files", models.ListHFFiles)
	g.POST("/models/hf/import", models.ImportHF)
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

	// Agent proxy
	g.GET("/agent/health", agent.Health)
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
	skills := v2.NewSkillsHandlerFull(svc, agentURL)
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
