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

// DeleteJob proxies DELETE /v1/ai/parser/jobs/{id} → /v1/parser/jobs/{id}.
func (p *ParserProxy) DeleteJob(c echo.Context) error {
	id := c.Param("id")
	body, code, err := p.Client.Forward("DELETE",
		"/v1/parser/jobs/"+id, "", nil)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	if len(body) == 0 {
		return c.NoContent(code)
	}
	return c.Blob(code, "application/json", body)
}

// ClearFailedJobs proxies POST /v1/ai/parser/jobs/clear-failed.
func (p *ParserProxy) ClearFailedJobs(c echo.Context) error {
	body, code, err := p.Client.Post("/v1/parser/jobs/clear-failed", nil)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// RetryJobs proxies POST /v1/ai/parser/jobs/retry → /v1/parser/jobs/retry.
// Body is {"file_ids": [...] | null}; null re-enqueues all failed jobs.
func (p *ParserProxy) RetryJobs(c echo.Context) error {
	raw, err := io.ReadAll(c.Request().Body)
	if err != nil {
		return c.JSON(400, echo.Map{"error": "read body: " + err.Error()})
	}
	body, code, err := p.Client.Post("/v1/parser/jobs/retry", raw)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// GetAllowlistExtensions proxies GET /v1/ai/parser/allowlist/extensions.
func (p *ParserProxy) GetAllowlistExtensions(c echo.Context) error {
	body, code, err := p.Client.Get("/v1/parser/allowlist/extensions")
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// PatchAllowlistExtension proxies PATCH /v1/ai/parser/allowlist/extensions.
func (p *ParserProxy) PatchAllowlistExtension(c echo.Context) error {
	raw, err := io.ReadAll(c.Request().Body)
	if err != nil {
		return c.JSON(400, echo.Map{"error": "read body: " + err.Error()})
	}
	body, code, err := p.Client.Forward("PATCH",
		"/v1/parser/allowlist/extensions", "application/json", raw)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// GetAllowlistFolders proxies GET /v1/ai/parser/allowlist/folders.
func (p *ParserProxy) GetAllowlistFolders(c echo.Context) error {
	body, code, err := p.Client.Get("/v1/parser/allowlist/folders")
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// PostAllowlistFolder proxies POST /v1/ai/parser/allowlist/folders.
func (p *ParserProxy) PostAllowlistFolder(c echo.Context) error {
	raw, err := io.ReadAll(c.Request().Body)
	if err != nil {
		return c.JSON(400, echo.Map{"error": "read body: " + err.Error()})
	}
	body, code, err := p.Client.Post("/v1/parser/allowlist/folders", raw)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// DeleteAllowlistFolder proxies DELETE /v1/ai/parser/allowlist/folders/{id}.
func (p *ParserProxy) DeleteAllowlistFolder(c echo.Context) error {
	id := c.Param("id")
	body, code, err := p.Client.Forward("DELETE",
		"/v1/parser/allowlist/folders/"+id, "", nil)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	if len(body) == 0 {
		return c.NoContent(code)
	}
	return c.Blob(code, "application/json", body)
}

// ListFiles forwards GET /v1/ai/parser/files → /v1/parser/files.
// Query string passes through verbatim.
func (p *ParserProxy) ListFiles(c echo.Context) error {
	path := "/v1/parser/files"
	if q := c.QueryString(); q != "" {
		path += "?" + q
	}
	body, code, err := p.Client.Get(path)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// ReindexFiles forwards POST /v1/ai/parser/files/reindex → /v1/parser/files/reindex.
// Body passes through verbatim (file_ids XOR filter, plus optional reason).
func (p *ParserProxy) ReindexFiles(c echo.Context) error {
	raw, err := io.ReadAll(c.Request().Body)
	if err != nil {
		return c.JSON(400, echo.Map{"error": "read body: " + err.Error()})
	}
	body, code, err := p.Client.Post("/v1/parser/files/reindex", raw)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// OcrModels proxies GET /v1/ai/parser/ocr/models → /v1/parser/ocr/models.
func (p *ParserProxy) OcrModels(c echo.Context) error {
	body, code, err := p.Client.Get("/v1/parser/ocr/models")
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// InstallOcrModel proxies POST /v1/ai/parser/ocr/models/{id}/install.
func (p *ParserProxy) InstallOcrModel(c echo.Context) error {
	id := c.Param("id")
	body, code, err := p.Client.Post("/v1/parser/ocr/models/"+id+"/install", nil)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// ActivateOcrModel proxies POST /v1/ai/parser/ocr/models/{id}/activate.
func (p *ParserProxy) ActivateOcrModel(c echo.Context) error {
	id := c.Param("id")
	body, code, err := p.Client.Post("/v1/parser/ocr/models/"+id+"/activate", nil)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	return c.Blob(code, "application/json", body)
}

// DeleteOcrModel proxies DELETE /v1/ai/parser/ocr/models/{id}.
func (p *ParserProxy) DeleteOcrModel(c echo.Context) error {
	id := c.Param("id")
	body, code, err := p.Client.Forward("DELETE",
		"/v1/parser/ocr/models/"+id, "", nil)
	if err != nil {
		return c.JSON(502, echo.Map{"error": err.Error()})
	}
	if len(body) == 0 {
		return c.NoContent(code)
	}
	return c.Blob(code, "application/json", body)
}
