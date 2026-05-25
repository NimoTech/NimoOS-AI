package v2

import (
	"encoding/json"

	"github.com/labstack/echo/v4"
)

type ParserClientIface interface {
	Get(path string) ([]byte, int, error)
	Post(path string, body []byte) ([]byte, int, error)
}

type ParserProxy struct {
	Client ParserClientIface
}

func (p *ParserProxy) Stats(c echo.Context) error {
	body, code, err := p.Client.Get("/v1/parser/stats")
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

func (p *ParserProxy) Jobs(c echo.Context) error {
	q := c.QueryString()
	path := "/v1/parser/jobs"
	if q != "" {
		path += "?" + q
	}
	body, code, err := p.Client.Get(path)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

func (p *ParserProxy) Folders(c echo.Context) error {
	q := c.QueryString()
	path := "/v1/parser/folders/pending"
	if q != "" {
		path += "?" + q
	}
	body, code, err := p.Client.Get(path)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

func (p *ParserProxy) State(c echo.Context) error {
	body, code, err := p.Client.Get("/v1/parser/control/state")
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

type controlReq struct {
	Action string `json:"action"`
	N      *int   `json:"n,omitempty"`
	Device string `json:"device,omitempty"`
}

func (p *ParserProxy) Control(c echo.Context) error {
	var req controlReq
	if err := c.Bind(&req); err != nil {
		return c.JSON(400, echo.Map{"error": "invalid json"})
	}
	switch req.Action {
	case "pause":
		body, code, err := p.Client.Post("/v1/parser/control/pause", nil)
		if err != nil {
			return c.JSON(502, echo.Map{"error": err.Error()})
		}
		return c.Blob(code, "application/json", body)
	case "resume":
		body, code, err := p.Client.Post("/v1/parser/control/resume", nil)
		if err != nil {
			return c.JSON(502, echo.Map{"error": err.Error()})
		}
		return c.Blob(code, "application/json", body)
	case "set_concurrency":
		if req.N == nil {
			return c.JSON(400, echo.Map{"error": "n required"})
		}
		b, _ := json.Marshal(echo.Map{"n": *req.N})
		body, code, err := p.Client.Post("/v1/parser/control/concurrency", b)
		if err != nil {
			return c.JSON(502, echo.Map{"error": err.Error()})
		}
		return c.Blob(code, "application/json", body)
	case "set_device":
		if req.Device == "" {
			return c.JSON(400, echo.Map{"error": "device required"})
		}
		b, _ := json.Marshal(echo.Map{"device": req.Device})
		body, code, err := p.Client.Post("/v1/parser/control/device", b)
		if err != nil {
			return c.JSON(502, echo.Map{"error": err.Error()})
		}
		return c.Blob(code, "application/json", body)
	default:
		return c.JSON(400, echo.Map{"error": "unknown action"})
	}
}
