package v2

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

// GetFile reads any file inside a bundle (read-only). Used by the UI to
// fetch SKILL.md, list files, or preview resources.
func (h *SkillsHandler) GetFile(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	id := c.Param("id")
	if err := service.ValidateSkillID(id); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	relPath := c.Param("*")
	if relPath == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "missing file path")
	}

	bundleDir, err := h.bundleDirFor(uid, id)
	if err != nil {
		return echo.NewHTTPError(http.StatusNotFound, err.Error())
	}
	data, err := h.svc.Skills().ReadFileInBundle(bundleDir, relPath)
	if errors.Is(err, service.ErrBadPath) {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if errors.Is(err, os.ErrNotExist) {
		return echo.NewHTTPError(http.StatusNotFound, err.Error())
	}
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	return c.Blob(http.StatusOK, "text/plain; charset=utf-8", data)
}

// ExportTarGz streams a tar.gz of the bundle.
func (h *SkillsHandler) ExportTarGz(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	id := c.Param("id")
	if err := service.ValidateSkillID(id); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	bundleDir, err := h.bundleDirFor(uid, id)
	if err != nil {
		return echo.NewHTTPError(http.StatusNotFound, err.Error())
	}
	c.Response().Header().Set(echo.HeaderContentType, "application/gzip")
	c.Response().Header().Set("Content-Disposition",
		"attachment; filename=\""+id+".tar.gz\"")
	c.Response().WriteHeader(http.StatusOK)
	gz := gzip.NewWriter(c.Response())
	tw := tar.NewWriter(gz)
	defer func() { tw.Close(); gz.Close() }()
	return filepath.Walk(bundleDir, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(bundleDir, p)
		if rel == "." {
			return nil
		}
		hdr, _ := tar.FileInfoHeader(info, "")
		hdr.Name = rel
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		f, err := os.Open(p)
		if err != nil {
			return err
		}
		defer f.Close()
		_, err = io.Copy(tw, f)
		return err
	})
}

// bundleDirFor finds either the built-in or the user's path for the skill.
func (h *SkillsHandler) bundleDirFor(uid, id string) (string, error) {
	store := h.svc.Skills().Store()
	if _, err := os.Stat(store.BuiltinPath(id)); err == nil {
		return store.BuiltinPath(id), nil
	}
	dir := store.UserPath(uid, id)
	if _, err := os.Stat(dir); err == nil {
		return dir, nil
	}
	return "", service.ErrSkillNotFound
}

type testStreamReq struct {
	Prompt  string `json:"prompt"`
	Network bool   `json:"network"`
}

// TestStream proxies the test request to the Python agent's
// /agent/sandbox-run SSE endpoint and pipes the event stream back to the
// browser unchanged.
//
// Fix 3.1: propagate the inbound request's Context so that when the
// browser disconnects (user closes the modal mid-stream), the upstream
// HTTP call cancels too — otherwise Python would keep burning LLM
// tokens on a run nobody's watching.
func (h *SkillsHandler) TestStream(c echo.Context) error {
	uid := c.Request().Header.Get("X-NimoOS-User-ID")
	if uid == "" {
		return echo.NewHTTPError(http.StatusUnauthorized, "missing user")
	}
	id := c.Param("id")
	if err := service.ValidateSkillID(id); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	var b testStreamReq
	if err := c.Bind(&b); err != nil {
		return echo.NewHTTPError(http.StatusBadRequest, err.Error())
	}
	if strings.TrimSpace(b.Prompt) == "" {
		return echo.NewHTTPError(http.StatusBadRequest, "empty prompt")
	}

	body, _ := json.Marshal(map[string]any{
		"skill_id": id, "prompt": b.Prompt, "network": b.Network,
	})
	reqURL := h.agentURL + "/agent/sandbox-run"
	upstream, err := http.NewRequestWithContext(
		c.Request().Context(), http.MethodPost, reqURL, bytes.NewReader(body))
	if err != nil {
		return echo.NewHTTPError(http.StatusInternalServerError, err.Error())
	}
	upstream.Header.Set("Content-Type", "application/json")
	upstream.Header.Set("X-User-Id", uid)
	upstream.Header.Set("X-User-Name", c.Request().Header.Get("X-NimoOS-User-Name"))
	for _, hdr := range []string{
		"X-Agent-Provider-Key", "X-Agent-Provider-Url", "X-Agent-Provider-Type",
	} {
		if v := c.Request().Header.Get(hdr); v != "" {
			upstream.Header.Set(hdr, v)
		}
	}

	resp, err := h.httpClient.Do(upstream)
	if err != nil {
		return echo.NewHTTPError(http.StatusBadGateway, err.Error())
	}
	defer resp.Body.Close()

	w := c.Response()
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(resp.StatusCode)
	_, err = io.Copy(w, resp.Body)
	return err
}
