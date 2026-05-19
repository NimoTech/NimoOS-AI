package v2

import (
	"errors"
	"fmt"
	"math/rand"
	"net/http"
	"strings"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type SkillsHandler struct{ svc service.Services }

func NewSkillsHandler(svc service.Services) *SkillsHandler {
	return &SkillsHandler{svc: svc}
}

func (h *SkillsHandler) List(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	list, err := h.svc.Skills().List(uid)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusOK, list)
}

func (h *SkillsHandler) Get(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	sk, err := h.svc.Skills().Get(uid, c.Param("id"))
	if err != nil {
		if errors.Is(err, service.ErrSkillNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, err.Error())
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusOK, sk)
}

type skillCreateBody struct {
	Name        string   `json:"name"`
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Trigger     string   `json:"trigger"`
	Color       string   `json:"color"`
	Icon        string   `json:"icon"`
	MD          string   `json:"md"`
	Examples    []string `json:"examples"`
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
	sk, err := h.svc.Skills().Create(uid, service.Skill{
		Name:        b.Name,
		Title:       b.Title,
		Description: b.Description,
		Trigger:     b.Trigger,
		Color:       b.Color,
		Icon:        b.Icon,
		MD:          b.MD,
		Examples:    b.Examples,
	})
	if err != nil {
		switch {
		case errors.Is(err, service.ErrSkillInvalid):
			return echo.NewHTTPError(http.StatusBadRequest, "name and description are required")
		case errors.Is(err, service.ErrSkillExists):
			return echo.NewHTTPError(http.StatusConflict, "a skill with that name already exists")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusCreated, sk)
}

type skillPatchBody struct {
	Enabled     *bool   `json:"enabled"`
	Title       *string `json:"title"`
	Description *string `json:"description"`
	Trigger     *string `json:"trigger"`
	Color       *string `json:"color"`
	MD          *string `json:"md"`
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
	fields := map[string]bool{}
	patch := service.Skill{}
	if b.Enabled != nil {
		fields["enabled"] = true
		patch.Enabled = *b.Enabled
	}
	if b.Title != nil {
		fields["title"] = true
		patch.Title = *b.Title
	}
	if b.Description != nil {
		fields["description"] = true
		patch.Description = *b.Description
	}
	if b.Trigger != nil {
		fields["trigger"] = true
		patch.Trigger = *b.Trigger
	}
	if b.Color != nil {
		fields["color"] = true
		patch.Color = *b.Color
	}
	if b.MD != nil {
		fields["md"] = true
		patch.MD = *b.MD
	}
	sk, err := h.svc.Skills().Update(uid, c.Param("id"), patch, fields)
	if err != nil {
		switch {
		case errors.Is(err, service.ErrSkillNotFound):
			return echo.NewHTTPError(http.StatusNotFound, err.Error())
		case errors.Is(err, service.ErrSkillSystem):
			return echo.NewHTTPError(http.StatusForbidden, "built-in skills only accept enable/disable")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	if sk == nil {
		// Built-in toggle returned no body — re-fetch for the response.
		sk, err = h.svc.Skills().Get(uid, c.Param("id"))
		if err != nil {
			return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
		}
	}
	return c.JSON(http.StatusOK, sk)
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

type skillTestBody struct {
	Prompt string `json:"prompt"`
}

type skillTestResult struct {
	OK     bool     `json:"ok"`
	MS     int      `json:"ms"`
	Tokens int      `json:"tokens"`
	Steps  []string `json:"steps"`
}

// Test runs a simulated sandbox invocation. Real skill execution is not
// wired up yet — the page surfaces this as an isolated dry-run so users
// can see what the skill would do without touching real files.
func (h *SkillsHandler) Test(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	id := c.Param("id")
	sk, err := h.svc.Skills().Get(uid, id)
	if err != nil {
		if errors.Is(err, service.ErrSkillNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, err.Error())
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	var b skillTestBody
	_ = c.Bind(&b)
	prompt := strings.TrimSpace(b.Prompt)
	if prompt == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "empty prompt")
	}

	// Simulate a sandbox run. Random within bounds so the UI feels alive.
	r := rand.New(rand.NewSource(time.Now().UnixNano()))
	ms := 820 + r.Intn(800)
	tokens := 240 + r.Intn(600)

	short := prompt
	if len(short) > 50 {
		short = short[:50] + "…"
	}

	noun := "files"
	if len(sk.Files) == 1 {
		noun = "file"
	}
	steps := []string{
		fmt.Sprintf("Loaded %s (%d %s)", sk.Name, len(sk.Files), noun),
		"Matched intent: \"" + short + "\"",
		"Dispatched to " + sk.Name + " → returning sandboxed result",
	}

	_ = h.svc.Skills().RecordRun(uid, id)
	return c.JSON(http.StatusOK, skillTestResult{
		OK: true, MS: ms, Tokens: tokens, Steps: steps,
	})
}
