package route

import (
	"net"
	"net/http"
	"strings"

	"github.com/labstack/echo/v4"
)

// LocalhostOnly rejects any request not originating from 127.0.0.1 or ::1.
// Use for /v1/ai/_internal/* routes that bypass JWT.
func LocalhostOnly(next echo.HandlerFunc) echo.HandlerFunc {
	return func(c echo.Context) error {
		if !isLocalhost(c) {
			return echo.NewHTTPError(http.StatusForbidden, "internal endpoint")
		}
		return next(c)
	}
}

func isLocalhost(c echo.Context) bool {
	host := c.Request().RemoteAddr
	if i := strings.LastIndex(host, ":"); i >= 0 {
		host = host[:i]
	}
	host = strings.Trim(host, "[]")
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	return ip.IsLoopback()
}
