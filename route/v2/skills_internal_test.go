package v2

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

// installBody builds a POST /_internal/skills/install request body.
func installReq(t *testing.T, userID string, skill map[string]any) *http.Request {
	t.Helper()
	body := map[string]any{"user_id": userID, "skill": skill}
	bb, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/skills/install", bytes.NewReader(bb))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	return req
}

func TestSkillsInstallInternal_CreatesAndListsForUser(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	req := installReq(t, "9", map[string]any{
		"name":        "lark-base",
		"title":       "Lark Base",
		"description": "Operate Feishu Base tables",
		"trigger":     "auto",
		"color":       "blue",
		"icon":        "sparkle",
		"md":          "## Lark Base\n\nDo stuff.",
		"examples":    []string{"list bases"},
	})
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := h.InstallInternal(c); err != nil {
		t.Fatalf("err: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	var out struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out.ID != "lark-base" {
		t.Fatalf("expected id=lark-base, got %q", out.ID)
	}

	list, err := h.svc.Skills().List("9")
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, sk := range list {
		if sk.ID == "lark-base" {
			found = true
			if !strings.Contains(sk.MD, "Do stuff") {
				t.Fatalf("expected MD to be persisted, got %q", sk.MD)
			}
		}
	}
	if !found {
		t.Fatalf("expected lark-base in runtime view, got %+v", list)
	}
}

func TestSkillsInstallInternal_RequiresUserID(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	req := installReq(t, "", map[string]any{"name": "x", "description": "d", "trigger": "auto"})
	rec := httptest.NewRecorder()
	err := h.InstallInternal(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestSkillsInstallInternal_SanitizesBadDescriptionInsteadOfRejecting(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	longDesc := strings.Repeat("a", 300) + "<script>\nsecond line"
	req := installReq(t, "9", map[string]any{
		"name":        "lark-doc",
		"description": longDesc,
		"trigger":     "auto",
		"md":          "## Lark Doc",
	})
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	if err := h.InstallInternal(c); err != nil {
		t.Fatalf("expected sanitize not reject, got err: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	list, err := h.svc.Skills().List("9")
	if err != nil {
		t.Fatal(err)
	}
	for _, sk := range list {
		if sk.ID == "lark-doc" {
			if len([]rune(sk.Description)) > 256 {
				t.Fatalf("description not truncated: %d runes", len([]rune(sk.Description)))
			}
			if strings.ContainsAny(sk.Description, "<>") {
				t.Fatalf("description still contains angle brackets: %q", sk.Description)
			}
			if strings.Contains(sk.Description, "\n") {
				t.Fatalf("description still multi-line: %q", sk.Description)
			}
			return
		}
	}
	t.Fatal("lark-doc not found after install")
}

func TestSkillsInstallInternal_IdempotentOverwrite(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	skill := map[string]any{
		"name": "lark-im", "description": "v1", "trigger": "auto", "md": "## v1",
	}
	rec1 := httptest.NewRecorder()
	if err := h.InstallInternal(e.NewContext(installReq(t, "9", skill), rec1)); err != nil {
		t.Fatalf("first install: %v", err)
	}
	if rec1.Code != http.StatusOK {
		t.Fatalf("first install code=%d body=%s", rec1.Code, rec1.Body.String())
	}

	skill["description"] = "v2"
	skill["md"] = "## v2"
	rec2 := httptest.NewRecorder()
	if err := h.InstallInternal(e.NewContext(installReq(t, "9", skill), rec2)); err != nil {
		t.Fatalf("second install: %v", err)
	}
	if rec2.Code != http.StatusOK {
		t.Fatalf("second install code=%d body=%s", rec2.Code, rec2.Body.String())
	}

	list, err := h.svc.Skills().List("9")
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, sk := range list {
		if sk.ID == "lark-im" {
			count++
			if sk.Description != "v2" {
				t.Fatalf("expected overwritten description v2, got %q", sk.Description)
			}
		}
	}
	if count != 1 {
		t.Fatalf("expected exactly 1 lark-im skill after re-install, got %d", count)
	}
}

func TestSkillsRemoveInternal_DeletesInstalled(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	rec := httptest.NewRecorder()
	if err := h.InstallInternal(e.NewContext(installReq(t, "9", map[string]any{
		"name": "lark-task", "description": "d", "trigger": "auto", "md": "## t",
	}), rec)); err != nil {
		t.Fatalf("install: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("install code=%d body=%s", rec.Code, rec.Body.String())
	}

	rmBody, _ := json.Marshal(map[string]any{"user_id": "9", "id": "lark-task"})
	rmReq := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/skills/remove", bytes.NewReader(rmBody))
	rmReq.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rmRec := httptest.NewRecorder()
	if err := h.RemoveInternal(e.NewContext(rmReq, rmRec)); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if rmRec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d body=%s", rmRec.Code, rmRec.Body.String())
	}

	list, err := h.svc.Skills().List("9")
	if err != nil {
		t.Fatal(err)
	}
	for _, sk := range list {
		if sk.ID == "lark-task" {
			t.Fatalf("expected lark-task removed, still present: %+v", sk)
		}
	}
}

func TestSkillsRemoveInternal_RequiresUserID(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	body, _ := json.Marshal(map[string]any{"id": "lark-task"})
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/skills/remove", bytes.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	err := h.RemoveInternal(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

func TestSkillsRemoveInternal_RequiresID(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	body, _ := json.Marshal(map[string]any{"user_id": "9"})
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/skills/remove", bytes.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	err := h.RemoveInternal(e.NewContext(req, rec))
	if he, ok := err.(*echo.HTTPError); !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

// F1 regression: a failed reinstall (bad content) must not lose the
// existing bundle. InstallOrReplace previously deleted the old user bundle
// before validating the new content, so a too-large SKILL.md on reinstall
// left the skill permanently gone.
func TestSkillsInstallInternal_FailedReinstallKeepsOldBundle(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	rec1 := httptest.NewRecorder()
	if err := h.InstallInternal(e.NewContext(installReq(t, "9", map[string]any{
		"name": "lark-approval", "description": "original", "trigger": "auto", "md": "## original",
	}), rec1)); err != nil {
		t.Fatalf("first install: %v", err)
	}
	if rec1.Code != http.StatusOK {
		t.Fatalf("first install code=%d body=%s", rec1.Code, rec1.Body.String())
	}

	oversized := strings.Repeat("x", service.MaxSkillMDBytes+10)
	rec2 := httptest.NewRecorder()
	err := h.InstallInternal(e.NewContext(installReq(t, "9", map[string]any{
		"name": "lark-approval", "description": "replacement", "trigger": "auto", "md": oversized,
	}), rec2))
	if err == nil {
		t.Fatalf("expected reinstall with oversized md to fail, got 200 body=%s", rec2.Body.String())
	}
	if he, ok := err.(*echo.HTTPError); !ok || he.Code == http.StatusOK {
		t.Fatalf("expected an error status, got %v", err)
	}

	list, lerr := h.svc.Skills().List("9")
	if lerr != nil {
		t.Fatal(lerr)
	}
	found := false
	for _, sk := range list {
		if sk.ID == "lark-approval" {
			found = true
			if sk.Description != "original" || !strings.Contains(sk.MD, "original") {
				t.Fatalf("expected original bundle intact, got description=%q md=%q", sk.Description, sk.MD)
			}
		}
	}
	if !found {
		t.Fatal("expected lark-approval to still exist after a failed reinstall, but it's gone")
	}
}

// F2 regression: installing a name that slugifies to an existing *built-in*
// skill id must 409 (matching the public POST /skills handler), not 500.
// newTestSkillsHandler seeds a "hello" built-in.
func TestSkillsInstallInternal_BuiltinCollisionReturns409(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	rec := httptest.NewRecorder()
	err := h.InstallInternal(e.NewContext(installReq(t, "9", map[string]any{
		"name": "hello", "description": "d", "trigger": "auto", "md": "## hello",
	}), rec))
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusConflict {
		t.Fatalf("expected 409, got %v", err)
	}
}

// F3 regression: user_id flows unvalidated into SkillsStore.UserPath, so a
// value containing path separators/traversal must be rejected before any
// disk I/O happens, for both install and remove.
func TestSkillsInstallInternal_RejectsBadUserID(t *testing.T) {
	h, store := newTestSkillsHandler(t)
	e := echo.New()
	rec := httptest.NewRecorder()
	err := h.InstallInternal(e.NewContext(installReq(t, "../../evil", map[string]any{
		"name": "lark-x", "description": "d", "trigger": "auto", "md": "## x",
	}), rec))
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
	if !strings.Contains(he.Message.(string), "bad_user_id") {
		t.Fatalf("expected bad_user_id message, got %v", he.Message)
	}
	escaped := filepath.Clean(filepath.Join(store.Root, "users", "../../evil"))
	if _, statErr := os.Stat(escaped); !os.IsNotExist(statErr) {
		t.Fatalf("expected no directory written outside skills root, stat err=%v", statErr)
	}
}

func TestSkillsRemoveInternal_RejectsBadUserID(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	body, _ := json.Marshal(map[string]any{"user_id": "../../evil", "id": "lark-x"})
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/skills/remove", bytes.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	err := h.RemoveInternal(e.NewContext(req, rec))
	he, ok := err.(*echo.HTTPError)
	if !ok || he.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %v", err)
	}
}

// A nonexistent id is not an error: service.Delete (shared with the public
// DELETE /skills/:id handler) treats "already gone" as success so a
// leftover/ghost state row can never make a skill undeletable. RemoveInternal
// mirrors that idempotent behavior rather than inventing a stricter contract.
func TestSkillsRemoveInternal_NonexistentIDIsNoop(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	body, _ := json.Marshal(map[string]any{"user_id": "9", "id": "does-not-exist"})
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/skills/remove", bytes.NewReader(body))
	req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
	rec := httptest.NewRecorder()
	if err := h.RemoveInternal(e.NewContext(req, rec)); err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if rec.Code != http.StatusNoContent {
		t.Fatalf("expected 204, got %d body=%s", rec.Code, rec.Body.String())
	}
}
