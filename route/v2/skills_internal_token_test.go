package v2

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/labstack/echo/v4"
)

// TestSkillsInstallInternal_RequiresInternalToken proves the wildcard-visible
// /_internal/skills/install route is gated by InternalTokenOnly (mirroring
// provider-credentials): user_id in the body means LocalhostOnly alone isn't
// a real boundary against an in-container sandbox process, so a missing or
// wrong X-Internal-Token must 401 before the handler ever runs.
func TestSkillsInstallInternal_RequiresInternalToken(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "ai_internal.token"), []byte("secret-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	e := echo.New()
	guarded := InternalTokenOnly(dir)(h.InstallInternal)

	body := map[string]any{"name": "x", "description": "d", "trigger": "auto", "md": "## x"}

	// No token at all -> 401, handler never invoked.
	rec := httptest.NewRecorder()
	if err := guarded(e.NewContext(installReq(t, "9", body), rec)); err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d body=%s", rec.Code, rec.Body.String())
	}

	// Wrong token -> 401.
	wrongReq := installReq(t, "9", body)
	wrongReq.Header.Set("X-Internal-Token", "not-the-token")
	rec2 := httptest.NewRecorder()
	if err := guarded(e.NewContext(wrongReq, rec2)); err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if rec2.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 with wrong token, got %d body=%s", rec2.Code, rec2.Body.String())
	}

	// Correct token -> passes through to the handler, 200.
	okReq := installReq(t, "9", body)
	okReq.Header.Set("X-Internal-Token", "secret-token")
	rec3 := httptest.NewRecorder()
	if err := guarded(e.NewContext(okReq, rec3)); err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if rec3.Code != http.StatusOK {
		t.Fatalf("expected 200 with correct token, got %d body=%s", rec3.Code, rec3.Body.String())
	}
}

// TestSkillsRemoveInternal_RequiresInternalToken mirrors the install case for
// the remove endpoint.
func TestSkillsRemoveInternal_RequiresInternalToken(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "ai_internal.token"), []byte("secret-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	e := echo.New()
	guarded := InternalTokenOnly(dir)(h.RemoveInternal)

	newRemoveReq := func(token string) *http.Request {
		b, _ := json.Marshal(map[string]any{"user_id": "9", "id": "does-not-exist"})
		req := httptest.NewRequest(http.MethodPost, "/v1/ai/_internal/skills/remove", bytes.NewReader(b))
		req.Header.Set(echo.HeaderContentType, echo.MIMEApplicationJSON)
		if token != "" {
			req.Header.Set("X-Internal-Token", token)
		}
		return req
	}

	rec := httptest.NewRecorder()
	if err := guarded(e.NewContext(newRemoveReq(""), rec)); err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d body=%s", rec.Code, rec.Body.String())
	}

	rec2 := httptest.NewRecorder()
	if err := guarded(e.NewContext(newRemoveReq("wrong"), rec2)); err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if rec2.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 with wrong token, got %d body=%s", rec2.Code, rec2.Body.String())
	}

	rec3 := httptest.NewRecorder()
	if err := guarded(e.NewContext(newRemoveReq("secret-token"), rec3)); err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if rec3.Code != http.StatusNoContent {
		t.Fatalf("expected 204 with correct token, got %d body=%s", rec3.Code, rec3.Body.String())
	}
}
