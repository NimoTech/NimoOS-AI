package v2

import (
	"database/sql"
	"errors"
	"net/http"
	"strconv"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

type ProvidersHandler struct{ svc service.Services }

func NewProvidersHandler(svc service.Services) *ProvidersHandler {
	return &ProvidersHandler{svc: svc}
}

type providerRequest struct {
	Name         *string           `json:"name"`
	BaseURL      *string           `json:"base_url"`
	APIKey       *string           `json:"api_key"` // plaintext; handler encrypts before storing
	Protocol     *service.Protocol `json:"protocol"`
	Enabled      *bool             `json:"enabled"`
	DefaultModel *string           `json:"default_model"`
}

// providerDTO is the response shape — never exposes the encrypted api_key.
type providerDTO struct {
	ID               int64            `json:"id"`
	Name             string           `json:"name"`
	BaseURL          string           `json:"base_url"`
	Protocol         service.Protocol `json:"protocol"`
	Enabled          bool             `json:"enabled"`
	HasKey           bool             `json:"has_key"`
	DefaultModel     string           `json:"default_model"`
	ProviderType     string           `json:"provider_type"`
	SupportsThinking bool             `json:"supports_thinking"`
}

func toDTO(p *service.Provider) providerDTO {
	return providerDTO{
		ID:               p.ID,
		Name:             p.Name,
		BaseURL:          p.BaseURL,
		Protocol:         p.Protocol,
		Enabled:          p.Enabled,
		HasKey:           p.APIKey != "",
		DefaultModel:     p.DefaultModel,
		ProviderType:     p.ProviderType,
		SupportsThinking: service.SupportsThinking(p.ProviderType, p.DefaultModel),
	}
}

func (h *ProvidersHandler) List(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	providers, err := h.svc.Providers().ListProviders(userID)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	result := make([]providerDTO, len(providers))
	for i, p := range providers {
		result[i] = toDTO(p)
	}
	return c.JSON(http.StatusOK, result)
}

func (h *ProvidersHandler) Create(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	var req providerRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	encKey := ""
	if req.APIKey != nil && *req.APIKey != "" {
		var err error
		encKey, err = h.svc.MasterKey().Encrypt(*req.APIKey)
		if err != nil {
			return echo.NewHTTPError(http.StatusInternalServerError, "failed to encrypt api key")
		}
	}
	if req.Protocol != nil && *req.Protocol != service.ProtocolOpenAI && *req.Protocol != service.ProtocolAnthropic {
		return echo.NewHTTPError(http.StatusBadRequest, "protocol must be 'openai' or 'anthropic'")
	}
	p := &service.Provider{UserID: userID, Protocol: service.ProtocolOpenAI}
	if req.Name != nil {
		p.Name = *req.Name
	}
	if req.BaseURL != nil {
		p.BaseURL = *req.BaseURL
	}
	if req.Protocol != nil {
		p.Protocol = *req.Protocol
	}
	if req.Enabled != nil {
		p.Enabled = *req.Enabled
	}
	if req.DefaultModel != nil {
		p.DefaultModel = *req.DefaultModel
	}
	p.APIKey = encKey
	if err := h.svc.Providers().CreateProvider(p); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusCreated, map[string]int64{"id": p.ID})
}

func (h *ProvidersHandler) Update(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	var req providerRequest
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}

	// Fetch existing record so unspecified fields are preserved.
	existing, err := h.svc.Providers().GetProvider(id, userID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "provider not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}

	// Merge: only overwrite fields that were explicitly provided.
	if req.Name != nil {
		existing.Name = *req.Name
	}
	if req.BaseURL != nil {
		existing.BaseURL = *req.BaseURL
	}
	if req.Protocol != nil {
		if *req.Protocol != service.ProtocolOpenAI && *req.Protocol != service.ProtocolAnthropic {
			return echo.NewHTTPError(http.StatusBadRequest, "protocol must be 'openai' or 'anthropic'")
		}
		existing.Protocol = *req.Protocol
	}
	if req.Enabled != nil {
		existing.Enabled = *req.Enabled
	}
	if req.DefaultModel != nil {
		existing.DefaultModel = *req.DefaultModel
	}
	if req.APIKey != nil && *req.APIKey != "" {
		encKey, err := h.svc.MasterKey().Encrypt(*req.APIKey)
		if err != nil {
			return echo.NewHTTPError(http.StatusInternalServerError, "failed to encrypt api key")
		}
		existing.APIKey = encKey
	}

	if err := h.svc.Providers().UpdateProvider(existing); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "provider not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}

func (h *ProvidersHandler) Delete(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	if err := h.svc.Providers().DeleteProvider(id, userID); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "provider not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}
