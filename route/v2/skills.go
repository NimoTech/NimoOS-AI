package v2

import (
	"errors"
	"net/http"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type SkillsHandler struct {
	svc        service.Services
	agentURL   string
	httpClient *http.Client
}

func NewSkillsHandler(svc service.Services) *SkillsHandler {
	return &SkillsHandler{svc: svc, httpClient: &http.Client{Timeout: 0}}
}

// NewSkillsHandlerFull wires the upstream Python agent URL for the
// streaming test endpoint. Production code uses this; unit tests that
// don't exercise streaming can use NewSkillsHandler.
func NewSkillsHandlerFull(svc service.Services, agentURL string) *SkillsHandler {
	return &SkillsHandler{svc: svc, agentURL: agentURL, httpClient: &http.Client{Timeout: 0}}
}

func (h *SkillsHandler) List(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	out, err := h.svc.Skills().List(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusOK, out)
}

func (h *SkillsHandler) Get(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	list, err := h.svc.Skills().List(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	id := c.Param("id")
	for _, sk := range list {
		if sk.ID == id {
			return c.JSON(http.StatusOK, sk)
		}
	}
	return echo.NewHTTPError(http.StatusNotFound, "not found")
}

type skillCreateBody struct {
	Name        string       `json:"name"`
	Title       string       `json:"title"`
	Description string       `json:"description"`
	Trigger     string       `json:"trigger"`
	Color       string       `json:"color"`
	Icon        string       `json:"icon"`
	MD          string       `json:"md"`
	Examples    []string     `json:"examples"`
	Scripts     []scriptFile `json:"scripts"`
}

type scriptFile struct {
	Path    string `json:"path"`
	Content string `json:"content"` // utf-8 text
}

func (h *SkillsHandler) Create(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	var b skillCreateBody
	if err := c.Bind(&b); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	files := make([]service.SkillFileUpload, 0, len(b.Scripts))
	for _, f := range b.Scripts {
		files = append(files, service.SkillFileUpload{Path: f.Path, Content: []byte(f.Content)})
	}
	sk, err := h.svc.Skills().CreateUser(uid, service.CreateSkillReq{
		Name: b.Name, Title: b.Title, Description: b.Description,
		Trigger: b.Trigger, Color: b.Color, Icon: b.Icon,
		MD: b.MD, Examples: b.Examples, Scripts: files,
	})
	if err != nil {
		switch {
		case errors.Is(err, service.ErrDuplicateSkill):
			return echo.NewHTTPError(http.StatusConflict, err.Error())
		case errors.Is(err, service.ErrBadSkillID),
			errors.Is(err, service.ErrBadPath),
			errors.Is(err, service.ErrBundleTooLarge):
			return echo.NewHTTPError(http.StatusBadRequest, err.Error())
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusCreated, sk)
}

type skillPatchBody struct {
	Enabled *bool `json:"enabled"`
}

func (h *SkillsHandler) Update(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	var b skillPatchBody
	if err := c.Bind(&b); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if b.Enabled == nil {
		return echo.NewHTTPError(http.StatusBadRequest, "only enabled is patchable")
	}
	if err := h.svc.Skills().SetEnabled(uid, c.Param("id"), *b.Enabled); err != nil {
		if errors.Is(err, service.ErrSkillNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, err.Error())
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return h.Get(c)
}

func (h *SkillsHandler) Delete(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	if err := h.svc.Skills().Delete(uid, c.Param("id")); err != nil {
		if errors.Is(err, service.ErrSkillNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, err.Error())
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}
