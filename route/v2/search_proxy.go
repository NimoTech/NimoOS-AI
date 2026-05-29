package v2

import (
	"io"
	"net/http"

	"github.com/labstack/echo/v4"
)

// SearchClientIface is the subset of *service.SearchClient the proxy needs.
type SearchClientIface interface {
	Forward(method, path, contentType string, body []byte) ([]byte, int, error)
}

// SearchProxy transparently forwards /v1/ai/search/* to the Search service's
// /v1/search/* endpoints (text, file, chunk, agent, ...). The Search service
// self-registers at the gateway under /v1/search, but the frontend speaks to
// the AI module under /v1/ai/search; this proxy bridges the two.
type SearchProxy struct {
	Client SearchClientIface
}

// Proxy forwards the request, rewriting /v1/ai/search/<rest> → /v1/search/<rest>,
// preserving method, query string, body, and Content-Type.
func (p *SearchProxy) Proxy(c echo.Context) error {
	rest := c.Param("*") // everything after /search/
	path := "/v1/search/" + rest
	if q := c.QueryString(); q != "" {
		path += "?" + q
	}
	var body []byte
	if c.Request().Body != nil {
		b, err := io.ReadAll(c.Request().Body)
		if err != nil {
			return c.JSON(http.StatusBadRequest, echo.Map{"error": "read body failed"})
		}
		body = b
	}
	ct := c.Request().Header.Get("Content-Type")
	resp, code, err := p.Client.Forward(c.Request().Method, path, ct, body)
	if err != nil {
		return c.JSON(http.StatusBadGateway, echo.Map{"error": err.Error()})
	}
	if len(resp) == 0 {
		return c.NoContent(code)
	}
	return c.Blob(code, "application/json", resp)
}
