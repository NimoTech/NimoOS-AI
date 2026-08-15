package v2

import (
	"errors"
	"net/http"
	"regexp"
	"strings"

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
			errors.Is(err, service.ErrBadDescription),
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

// --- internal loopback (no JWT; localhost-only via _internal group) ---
// user_id comes from the request body (caller is a local, trusted process —
// the Python agent registering a skill bundled with a Go component such as
// the Lark integration), NOT from X-NimoOS-User-ID. Mirrors the
// ParseInternal/RegisterInternal shape in route/v2/mcp.go.
//
// The agent container's skills tree is mounted read-only, so Python can't
// write manifest.json/SKILL.md itself; it calls back into Go instead.

type internalSkillInstallBody struct {
	UserID string          `json:"user_id"`
	Skill  skillCreateBody `json:"skill"`
}

// internalUserIDRe bounds the internal-endpoint user_id the same way a real
// user id looks (JWT claims.ID is a decimal string today, see route/v2.go),
// but is intentionally permissive enough for other trusted local callers.
// This isn't cosmetic: unlike the public endpoints (user_id comes from a
// verified JWT), these two loopback endpoints take user_id straight from the
// request body, and it flows unvalidated into SkillsStore.UserPath(userID,
// id) = filepath.Join(root, "users", userID, id). A value like "../../evil"
// would let a caller escape the skills root entirely. No dots, slashes, or
// other path metacharacters allowed.
var internalUserIDRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

func validInternalUserID(uid string) bool {
	return internalUserIDRe.MatchString(uid)
}

// sanitizeSkillDescription cleans an upstream-supplied skill description for
// safe injection into the agent's system prompt, mirroring the rules
// validateSkillDescription (skills_store.go) enforces on the public
// endpoint. Descriptions arriving here come from official Go component
// content we don't control, so instead of rejecting bad input with 400 we
// sanitize it: newlines are folded to spaces, control characters are
// dropped, angle brackets are replaced (not stripped, so meaning survives),
// and the result is capped at 256 runes.
func sanitizeSkillDescription(d string) string {
	var b strings.Builder
	for _, r := range d {
		switch {
		case r == '\n' || r == '\r':
			b.WriteRune(' ')
		case r == '<':
			b.WriteRune('(')
		case r == '>':
			b.WriteRune(')')
		case r < 0x20 || r == 0x7f:
			// drop control characters
		default:
			b.WriteRune(r)
		}
	}
	out := strings.TrimSpace(b.String())
	if runes := []rune(out); len(runes) > 256 {
		out = string(runes[:256])
	}
	return out
}

// InstallInternal handles POST /v1/ai/_internal/skills/install — registers
// (or, on repeat calls with the same name, overwrites) a skill bundle owned
// by a Go component into the given user's skill store. Used by Task 9's
// Python-side component registration on agent startup.
func (h *SkillsHandler) InstallInternal(c echo.Context) error {
	var b internalSkillInstallBody
	if err := c.Bind(&b); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if strings.TrimSpace(b.UserID) == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "user_id required")
	}
	if !validInternalUserID(b.UserID) {
		return echo.NewHTTPError(http.StatusBadRequest, "bad_user_id")
	}
	sk := b.Skill
	sk.Description = sanitizeSkillDescription(sk.Description)
	files := make([]service.SkillFileUpload, 0, len(sk.Scripts))
	for _, f := range sk.Scripts {
		files = append(files, service.SkillFileUpload{Path: f.Path, Content: []byte(f.Content)})
	}
	out, err := h.svc.Skills().InstallOrReplace(b.UserID, service.CreateSkillReq{
		Name: sk.Name, Title: sk.Title, Description: sk.Description,
		Trigger: sk.Trigger, Color: sk.Color, Icon: sk.Icon,
		MD: sk.MD, Examples: sk.Examples, Scripts: files,
	})
	if err != nil {
		switch {
		case errors.Is(err, service.ErrDuplicateSkill):
			// The slugified name collides with a *built-in* skill id (the
			// only case InstallOrReplace doesn't pre-empt by deleting: it
			// only removes an existing *user* bundle at that id before
			// retrying). Align with the public POST /skills handler's 409.
			return echo.NewHTTPError(http.StatusConflict, err.Error())
		case errors.Is(err, service.ErrBadSkillID),
			errors.Is(err, service.ErrBadDescription),
			errors.Is(err, service.ErrBadPath),
			errors.Is(err, service.ErrBundleTooLarge):
			return echo.NewHTTPError(http.StatusBadRequest, err.Error())
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.JSON(http.StatusOK, map[string]string{"id": out.ID})
}

// RemoveInternal handles POST /v1/ai/_internal/skills/remove — same as the
// public DELETE /skills/:id, but user_id comes from the body instead of
// X-NimoOS-User-ID (see InstallInternal).
func (h *SkillsHandler) RemoveInternal(c echo.Context) error {
	var b struct {
		UserID string `json:"user_id"`
		ID     string `json:"id"`
	}
	if err := c.Bind(&b); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if strings.TrimSpace(b.UserID) == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "user_id required")
	}
	if !validInternalUserID(b.UserID) {
		return echo.NewHTTPError(http.StatusBadRequest, "bad_user_id")
	}
	if strings.TrimSpace(b.ID) == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "id required")
	}
	if err := h.svc.Skills().Delete(b.UserID, b.ID); err != nil {
		if errors.Is(err, service.ErrSkillNotFound) {
			return echo.NewHTTPError(http.StatusNotFound, err.Error())
		}
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.NoContent(http.StatusNoContent)
}
