package v2

import (
	"archive/tar"
	"compress/gzip"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"

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
