package v2

import (
	"context"
	"log"
	"net/http"
	"net/url"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type ModelsHandler struct {
	svc      service.Services
	modelDir string
}

func NewModelsHandler(svc service.Services, modelDir string) *ModelsHandler {
	return &ModelsHandler{svc: svc, modelDir: modelDir}
}

func (h *ModelsHandler) List(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	models, err := h.svc.ModelManager().ListModels()
	if err != nil {
		return echo.NewHTTPError(http.StatusServiceUnavailable, "ollama unavailable: "+err.Error())
	}
	return c.JSON(http.StatusOK, models)
}

func (h *ModelsHandler) Pull(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	var req struct {
		Name string `json:"name"`
	}
	if err := c.Bind(&req); err != nil || req.Name == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "name is required")
	}
	go func() {
		progress := make(chan service.PullProgress, 20)
		go func() { for range progress {} }() // drain progress
		if err := h.svc.ModelManager().PullModel(req.Name, progress); err != nil {
			log.Printf("PullModel %q: %v", req.Name, err)
		}
		close(progress)
	}()
	return c.JSON(http.StatusAccepted, map[string]string{"status": "pulling", "name": req.Name})
}

func (h *ModelsHandler) SearchHF(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	query := c.QueryParam("q")
	if query == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "q is required")
	}
	results, err := h.svc.ModelManager().SearchHuggingFace(query)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadGateway, "HuggingFace search failed: "+err.Error())
	}
	return c.JSON(http.StatusOK, results)
}

func (h *ModelsHandler) ListHFFiles(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	repo := c.QueryParam("repo")
	if repo == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "repo is required")
	}
	files, err := h.svc.ModelManager().ListGGUFFiles(repo)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadGateway, "HuggingFace query failed: "+err.Error())
	}
	return c.JSON(http.StatusOK, files)
}

func (h *ModelsHandler) ImportHF(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	var req struct {
		Repo     string `json:"repo"`
		Filename string `json:"filename"`
	}
	if err := c.Bind(&req); err != nil || req.Repo == "" || req.Filename == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "repo and filename are required")
	}
	ctx, cancel := context.WithCancel(context.Background())
	h.svc.ModelManager().StartImportJob(req.Repo, req.Filename, cancel)
	go func() {
		err := h.svc.ModelManager().ImportFromHuggingFace(ctx, req.Repo, req.Filename, h.modelDir)
		if err != nil {
			log.Printf("ImportFromHuggingFace %q/%q: %v", req.Repo, req.Filename, err)
		}
		h.svc.ModelManager().FinishImportJob(req.Filename, err)
	}()
	return c.JSON(http.StatusAccepted, map[string]string{"status": "importing", "filename": req.Filename})
}

func (h *ModelsHandler) ImportStatus(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	filename := c.QueryParam("filename")
	if filename == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "filename is required")
	}
	snap, ok := h.svc.ModelManager().GetImportStatus(filename)
	if !ok {
		return echo.NewHTTPError(http.StatusNotFound, "no active import for this filename")
	}
	return c.JSON(http.StatusOK, snap)
}

func (h *ModelsHandler) CancelImport(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	filename := c.QueryParam("filename")
	if filename == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "filename is required")
	}
	h.svc.ModelManager().CancelImport(filename)
	return c.NoContent(http.StatusNoContent)
}

// ListInternal handles GET /v1/ai/_internal/models.
//
// Unlike List, this variant does NOT require JWT (the _internal group is
// localhost-only) and returns BOTH local Ollama models AND the user's
// enabled cloud providers. Wiki summary worker uses this to auto-detect
// what model to use:
//   - prefer local (cheapest),
//   - fall back to cloud if Ollama is empty/unavailable.
func (h *ModelsHandler) ListInternal(c echo.Context) error {
	userID := c.QueryParam("user_id")
	if userID == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "missing user_id query param")
	}

	var localOut []map[string]any
	if models, err := h.svc.ModelManager().ListModels(); err == nil {
		for _, m := range models {
			localOut = append(localOut, map[string]any{
				"name":              m.Name,
				"size_bytes":        m.SizeBytes,
				"supports_thinking": m.SupportsThinking,
			})
		}
	}
	if localOut == nil {
		localOut = []map[string]any{}
	}

	var cloudOut []map[string]any
	if providers, err := h.svc.Providers().ListProviders(userID); err == nil {
		for _, p := range providers {
			if !p.Enabled {
				continue
			}
			cloudOut = append(cloudOut, map[string]any{
				"provider_id":   p.ID,
				"provider_name": p.Name,
				"default_model": p.DefaultModel,
				"enabled":       p.Enabled,
			})
		}
	}
	if cloudOut == nil {
		cloudOut = []map[string]any{}
	}

	return c.JSON(http.StatusOK, map[string]any{
		"local": localOut,
		"cloud": cloudOut,
	})
}

func (h *ModelsHandler) Delete(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	name := c.Param("name")
	if name == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "name is required")
	}
	decoded, err := url.PathUnescape(name)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid name encoding")
	}
	name = decoded
	if err := h.svc.ModelManager().DeleteModel(name); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}
