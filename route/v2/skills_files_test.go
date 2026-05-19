package v2

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

func newTestSkillsHandler(t *testing.T) (*SkillsHandler, *service.SkillsStore) {
	t.Helper()
	root := t.TempDir()
	dbPath := filepath.Join(root, "ai.db")
	db, err := service.NewDB(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	store := &service.SkillsStore{Root: filepath.Join(root, "skills")}
	_ = os.MkdirAll(store.BuiltinPath("hello"), 0o755)
	_ = os.WriteFile(filepath.Join(store.BuiltinPath("hello"), "manifest.json"), []byte(`{
		"schema_version":1,"id":"hello","name":"hello","title":"Hello",
		"trigger":"auto","color":"blue","icon":"sparkle","description":"d",
		"version":"0.1.0","author":"Nimo","examples":[]}`), 0o644)
	_ = os.WriteFile(filepath.Join(store.BuiltinPath("hello"), "SKILL.md"), []byte("## hi"), 0o644)
	svc := service.NewServiceFromParts(db, store)
	return NewSkillsHandler(svc), store
}

func TestSkillsFiles_GetFile(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/skills/hello/files/SKILL.md", nil)
	req.Header.Set("X-NimoOS-User-ID", "42")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id", "*")
	c.SetParamValues("hello", "SKILL.md")
	if err := h.GetFile(c); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "## hi") {
		t.Fatalf("got %d / %s", rec.Code, rec.Body.String())
	}
}

func TestSkillsFiles_GetFile_RejectsTraversal(t *testing.T) {
	h, _ := newTestSkillsHandler(t)
	e := echo.New()
	req := httptest.NewRequest(http.MethodGet, "/skills/hello/files/../../etc/passwd", nil)
	req.Header.Set("X-NimoOS-User-ID", "42")
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id", "*")
	c.SetParamValues("hello", "../../etc/passwd")
	if err := h.GetFile(c); err == nil {
		t.Fatal("expected refusal")
	}
}
