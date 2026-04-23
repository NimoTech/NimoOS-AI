package v2

import (
	"net/http"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type PolicyHandler struct{ svc service.Services }

func NewPolicyHandler(svc service.Services) *PolicyHandler {
	return &PolicyHandler{svc: svc}
}

func (h *PolicyHandler) Get(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	policy, err := h.svc.Providers().GetPolicy(userID)
	if err != nil {
		// No record — return default values
		return c.JSON(http.StatusOK, service.PrivacyPolicy{
			UserID: userID, AllowRemote: true, DefaultBackend: "local", EscalationPrompt: true,
		})
	}
	return c.JSON(http.StatusOK, policy)
}

func (h *PolicyHandler) Update(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	var policy service.PrivacyPolicy
	if err := c.Bind(&policy); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	policy.UserID = userID // enforce user_id from header, not body
	if err := h.svc.Providers().UpsertPolicy(&policy); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}
