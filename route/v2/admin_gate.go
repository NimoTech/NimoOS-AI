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

// enforceAdmin reports whether the caller is an admin. When it is not, the
// denial response (403, or 503 when UserService cannot be consulted — fail
// closed, never open) has already been written and the returned error is what
// the caller must return; `allowed` is the decision, NOT the error, because
// c.JSON returns a nil error after successfully writing a 403. Shared by
// AdminOnly (per route) and AdminPathGuard (per decoded path).
func enforceAdmin(c echo.Context, runtimePath string) (allowed bool, err error) {
	raw, err := os.ReadFile(
		filepath.Join(runtimePath, external.UserServiceAddressFilename))
	if err != nil {
		return false, c.JSON(http.StatusServiceUnavailable,
			map[string]string{"message": "user service unavailable"})
	}
	ok, err := IsAdmin(strings.TrimSpace(string(raw)),
		c.Request().Header.Get(echo.HeaderAuthorization))
	if err != nil {
		return false, c.JSON(http.StatusServiceUnavailable,
			map[string]string{"message": "user service unavailable"})
	}
	if !ok {
		return false, c.JSON(http.StatusForbidden,
			map[string]string{"message": "admin required"})
	}
	return true, nil
}

// AdminOnly gates a route on the caller being a NimoOS admin. Attached to each
// path in AdminScopedAgentPaths; AdminPathGuard covers the same paths spelled
// with percent-encoding, which Echo routes elsewhere.
func AdminOnly(runtimePath string) echo.MiddlewareFunc {
	return func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c echo.Context) error {
			// The guard runs first (e.Use, before route middleware) and has
			// already asked UserService for this very request when it fired;
			// asking again would double every admin request's latency.
			if v, ok := c.Get(adminCheckedContextKey).(bool); ok && v {
				return next(c)
			}
			allowed, err := enforceAdmin(c, runtimePath)
			if !allowed {
				return err
			}
			return next(c)
		}
	}
}
