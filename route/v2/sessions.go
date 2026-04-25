package v2

import (
	"errors"
	"net/http"
	"strconv"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type SessionsHandler struct {
	svc service.Services
}

func NewSessionsHandler(svc service.Services) *SessionsHandler {
	return &SessionsHandler{svc: svc}
}

func (h *SessionsHandler) List(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	sessions, err := h.svc.Sessions().ListSessions(userID)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	if sessions == nil {
		sessions = []*service.ChatSession{}
	}
	return c.JSON(http.StatusOK, sessions)
}

func (h *SessionsHandler) Create(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	var body struct {
		Title string `json:"title"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if body.Title == "" {
		body.Title = "新会话"
	}
	sess, err := h.svc.Sessions().CreateSession(userID, body.Title)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusCreated, sess)
}

func (h *SessionsHandler) Delete(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid session id")
	}
	if err := h.svc.Sessions().DeleteSession(userID, id); err != nil {
		if errors.Is(err, service.ErrSessionNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, "session not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

func (h *SessionsHandler) ListMessages(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid session id")
	}
	msgs, err := h.svc.Sessions().ListMessages(userID, id)
	if err != nil {
		if errors.Is(err, service.ErrSessionNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, "session not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	if msgs == nil {
		msgs = []*service.ChatMessage{}
	}
	return c.JSON(http.StatusOK, msgs)
}

func (h *SessionsHandler) AppendMessages(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid session id")
	}
	var body []service.ChatMessage
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if err := h.svc.Sessions().AppendMessages(userID, id, body); err != nil {
		if errors.Is(err, service.ErrSessionNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, "session not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

func (h *SessionsHandler) UpdateTitle(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid session id")
	}
	var body struct {
		Title string `json:"title"`
	}
	if err := c.Bind(&body); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if err := h.svc.Sessions().UpdateTitle(userID, id, body.Title); err != nil {
		if errors.Is(err, service.ErrSessionNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, "session not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}
