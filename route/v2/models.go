package v2

import (
	"net/http"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type ModelsHandler struct{ svc service.Services }

func NewModelsHandler(svc service.Services) *ModelsHandler {
	return &ModelsHandler{svc: svc}
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
		defer close(progress)
		_ = h.svc.ModelManager().PullModel(req.Name, progress)
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
	modelDir := "/var/lib/nimoos/ai/models"
	go func() {
		progress := make(chan service.PullProgress, 50)
		defer close(progress)
		_ = h.svc.ModelManager().ImportFromHuggingFace(req.Repo, req.Filename, modelDir, progress)
	}()
	return c.JSON(http.StatusAccepted, map[string]string{"status": "importing", "filename": req.Filename})
}

func (h *ModelsHandler) Delete(c echo.Context) error {
	if c.Request().Header.Get("X-NimoOS-User-ID") == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	name := c.Param("name")
	if name == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "name is required")
	}
	if err := h.svc.ModelManager().DeleteModel(name); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}
