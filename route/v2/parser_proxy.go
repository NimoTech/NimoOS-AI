package v2

import (
	"encoding/json"
	"io"
	"net/http"

	"github.com/labstack/echo/v4"
)

type ParserClientIface interface {
	Get(path string) ([]byte, int, error)
	Post(path string, body []byte) ([]byte, int, error)
	Forward(method, path, contentType string, body []byte) ([]byte, int, error)
}

type ParserProxy struct {
	Client ParserClientIface
}

// TestAnalyze forwards a multipart upload to /v1/parser/test/analyze.
// Caps the upload at 32 MiB to match Parser's 30 MiB cap plus form overhead.
func (p *ParserProxy) TestAnalyze(c echo.Context) error {
	const maxBody = 32 * 1024 * 1024
	c.Request().Body = http.MaxBytesReader(c.Response().Writer, c.Request().Body, maxBody)
	body, err := io.ReadAll(c.Request().Body)
	if err != nil {
		return c.JSON(413, echo.Map{"error": "upload too large or read failed"})
	}
	ct := c.Request().Header.Get("Content-Type")
	resp, code, err := p.Client.Forward("POST", "/v1/parser/test/analyze", ct, body)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", resp)
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
	Action  string `json:"action"`
	N       *int   `json:"n,omitempty"`
	Device  string `json:"device,omitempty"`
	Enabled *bool  `json:"enabled,omitempty"`
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
	case "set_ocr":
		if req.Enabled == nil {
			return c.JSON(400, echo.Map{"error": "enabled required"})
		}
		b, _ := json.Marshal(echo.Map{"enabled": *req.Enabled})
		body, code, err := p.Client.Post("/v1/parser/control/ocr", b)
		if err != nil {
			return c.JSON(502, echo.Map{"error": err.Error()})
		}
		return c.Blob(code, "application/json", body)
	default:
		return c.JSON(400, echo.Map{"error": "unknown action"})
	}
}
