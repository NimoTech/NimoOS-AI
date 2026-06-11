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

// providerModelDTO is one model in the provider's catalogue.
type providerModelDTO struct {
	Name             string `json:"name"`
	Source           string `json:"source"`
	Favorite         bool   `json:"favorite"`
	SupportsThinking bool   `json:"supports_thinking"`
}

// providerDTO is the response shape — never exposes the encrypted api_key.
// Models embeds ONLY favorites (drives the ModelPicker); the full catalogue is
// fetched on demand via GET /providers/:id/models.
type providerDTO struct {
	ID               int64              `json:"id"`
	Name             string             `json:"name"`
	BaseURL          string             `json:"base_url"`
	Protocol         service.Protocol   `json:"protocol"`
	Enabled          bool               `json:"enabled"`
	HasKey           bool               `json:"has_key"`
	DefaultModel     string             `json:"default_model"`
	ProviderType     string             `json:"provider_type"`
	SupportsThinking bool               `json:"supports_thinking"`
	Models           []providerModelDTO `json:"models"`
}

func modelToDTO(pt string, m *service.ProviderModel) providerModelDTO {
	return providerModelDTO{
		Name:             m.ModelName,
		Source:           m.Source,
		Favorite:         m.Favorite,
		SupportsThinking: service.SupportsThinking(pt, m.ModelName),
	}
}

func (h *ProvidersHandler) toDTO(p *service.Provider) providerDTO {
	favs, _ := h.svc.Providers().ListFavoriteModels(p.ID)
	models := make([]providerModelDTO, 0, len(favs))
	for _, m := range favs {
		models = append(models, modelToDTO(p.ProviderType, m))
	}
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
		Models:           models,
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
		result[i] = h.toDTO(p)
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
	// Synchronous first fetch (capped at 8s inside FetchModels). Non-fatal:
	// the provider is created regardless; warn the client if discovery failed.
	warning := ""
	if plainKey, derr := h.decryptKey(p.APIKey); derr == nil {
		if names, ferr := service.FetchModels(p, plainKey); ferr == nil {
			_ = h.svc.Providers().UpsertFetchedModels(p.ID, names)
		} else {
			warning = "model discovery failed; add models manually or refresh"
		}
	}
	return c.JSON(http.StatusCreated, map[string]interface{}{"id": p.ID, "warning": warning})
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

// decryptKey returns the plaintext API key, or "" with nil error when empty.
func (h *ProvidersHandler) decryptKey(enc string) (string, error) {
	if enc == "" {
		return "", nil
	}
	return h.svc.MasterKey().Decrypt(enc)
}

// ListModels handles GET /v1/ai/providers/:id/models — full catalogue, chat
// models first (non-destructive ordering).
func (h *ProvidersHandler) ListModels(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	p, err := h.svc.Providers().GetProvider(id, userID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "provider not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	models, err := h.svc.Providers().ListModels(id)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusOK, sortModelDTOs(p.ProviderType, models))
}

// RefreshModels handles POST /v1/ai/providers/:id/models/refresh.
func (h *ProvidersHandler) RefreshModels(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	p, err := h.svc.Providers().GetProvider(id, userID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "provider not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	plainKey, err := h.decryptKey(p.APIKey)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, "failed to decrypt api key")
	}
	names, ferr := service.FetchModels(p, plainKey)
	if ferr != nil {
		return echo.NewHTTPError(http.StatusBadGateway, ferr.Error())
	}
	if err := h.svc.Providers().UpsertFetchedModels(id, names); err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	models, err := h.svc.Providers().ListModels(id)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusOK, sortModelDTOs(p.ProviderType, models))
}

// UpdateModels handles PUT /v1/ai/providers/:id/models — favorite toggles +
// manual add/delete. Source is read-only (anti-tamper, enforced in store).
func (h *ProvidersHandler) UpdateModels(c echo.Context) error {
	userID := c.Request().Header.Get("X-NimoOS-User-ID")
	if userID == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user identity")
	}
	id, err := strconv.ParseInt(c.Param("id"), 10, 64)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, "invalid id")
	}
	p, err := h.svc.Providers().GetProvider(id, userID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return echo.NewHTTPError(http.StatusNotFound, "provider not found")
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	var req struct {
		Models []service.ProviderModelInput `json:"models"`
	}
	if err := c.Bind(&req); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	models, err := h.svc.Providers().ReconcileModels(id, req.Models)
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusOK, sortModelDTOs(p.ProviderType, models))
}

// sortModelDTOs converts and orders chat-like models first (non-destructive —
// nothing is hidden, only ordered). Stable within each group by original order.
func sortModelDTOs(pt string, models []*service.ProviderModel) []providerModelDTO {
	chat := make([]providerModelDTO, 0, len(models))
	other := make([]providerModelDTO, 0, len(models))
	for _, m := range models {
		dto := modelToDTO(pt, m)
		if service.LooksLikeChatModel(pt, m.ModelName) {
			chat = append(chat, dto)
		} else {
			other = append(other, dto)
		}
	}
	return append(chat, other...)
}
