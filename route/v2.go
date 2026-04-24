package route

import (
	"crypto/ecdsa"
	"net/http"
	"strconv"
	"strings"

	"github.com/NimoTech/NimoOS-AI/common"
	v2 "github.com/NimoTech/NimoOS-AI/route/v2"
	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/NimoTech/NimoOS-Common/external"
	"github.com/NimoTech/NimoOS-Common/utils/jwt"
	"github.com/labstack/echo/v4"
	echo_middleware "github.com/labstack/echo/v4/middleware"
)

func InitV2Router(svc service.Services, runtimePath string) http.Handler {
	chat := v2.NewChatHandler(svc)
	providers := v2.NewProvidersHandler(svc)
	policy := v2.NewPolicyHandler(svc)
	models := v2.NewModelsHandler(svc)

	e := echo.New()
	e.Use(echo_middleware.CORSWithConfig(echo_middleware.CORSConfig{
		AllowOrigins: []string{"*"},
		AllowMethods: []string{echo.POST, echo.GET, echo.PUT, echo.DELETE, echo.OPTIONS},
		AllowHeaders: []string{echo.HeaderAuthorization, echo.HeaderContentType, "X-NimoOS-Force-Cloud"},
	}))

	e.Use(echo_middleware.JWTWithConfig(echo_middleware.JWTConfig{
		Skipper: func(c echo.Context) bool {
			return false // no localhost exemption: prevents unauthorized access to other users' cloud API keys
		},
		ParseTokenFunc: func(token string, c echo.Context) (interface{}, error) {
			valid, claims, err := jwt.Validate(token, func() (*ecdsa.PublicKey, error) {
				return external.GetPublicKey(runtimePath)
			})
			if err != nil || !valid {
				return nil, echo.ErrUnauthorized
			}
			c.Request().Header.Set("X-NimoOS-User-ID", strconv.Itoa(claims.ID))
			return claims, nil
		},
		TokenLookupFuncs: []echo_middleware.ValuesExtractor{
			func(c echo.Context) ([]string, error) {
				auth := c.Request().Header.Get(echo.HeaderAuthorization)
				return []string{strings.TrimPrefix(auth, "Bearer ")}, nil
			},
		},
	}))

	g := e.Group(common.V2APIPath)

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

	return e
}
