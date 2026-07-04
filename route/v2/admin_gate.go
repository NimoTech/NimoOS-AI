package v2

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/NimoTech/NimoOS-Common/external"
	"github.com/labstack/echo/v4"
)

const adminRole = "admin"

// IsAdmin asks UserService whether the caller of authHeader is an admin.
// JWT claims carry no role, so this lookup is the established pattern
// (mirrors NimoOS-Terminal/auth/admin.go).
func IsAdmin(userServiceBaseURL, authHeader string) (bool, error) {
	req, err := http.NewRequest(http.MethodGet,
		strings.TrimRight(userServiceBaseURL, "/")+"/v1/users/current", nil)
	if err != nil {
		return false, err
	}
	req.Header.Set("Authorization", authHeader)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("user service status %d", resp.StatusCode)
	}
	var body struct {
		Data struct {
			Role string `json:"role"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return false, err
	}
	return body.Data.Role == adminRole, nil
}

// AdminOnly gates a route on the caller being a NimoOS admin. Used for
// channel instance management (system-scoped bot config).
func AdminOnly(runtimePath string) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			raw, err := os.ReadFile(
				filepath.Join(runtimePath, external.UserServiceAddressFilename))
			if err != nil {
				return c.JSON(http.StatusServiceUnavailable,
					map[string]string{"message": "user service unavailable"})
			}
			ok, err := IsAdmin(strings.TrimSpace(string(raw)),
				c.Request().Header.Get(echo.HeaderAuthorization))
			if err != nil {
				return c.JSON(http.StatusServiceUnavailable,
					map[string]string{"message": "user service unavailable"})
			}
			if !ok {
				return c.JSON(http.StatusForbidden,
					map[string]string{"message": "admin required"})
			}
			return next(c)
		}
	}
}
