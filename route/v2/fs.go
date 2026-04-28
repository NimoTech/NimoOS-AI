package v2

import (
	"net/http"
	"os"
	"strings"
	"syscall"

	"github.com/labstack/echo/v4"
)

type Mount struct {
	Path  string `json:"path"`
	Label string `json:"label"`
	Type  string `json:"type"`            // 'system' | 'raid' | 'disk'
	Total uint64 `json:"total,omitempty"` // bytes
	Used  uint64 `json:"used,omitempty"`  // bytes
}

type FSHandler struct{}

func NewFSHandler() *FSHandler { return &FSHandler{} }

// Mounts returns the maximum visible scope for the picker.
// Exposed: /DATA (system) plus the configured RAID/disk mount points under /mnt.
// This is intentionally a thin wrapper over the well-known layout; future work
// can swap to NimoOS-LocalStorage when its API is mature.
func (h *FSHandler) Mounts(c echo.Context) error {
	out := []Mount{}
	if total, used, ok := stat("/DATA"); ok {
		out = append(out, Mount{
			Path: "/DATA", Label: "System (/DATA)", Type: "system",
			Total: total, Used: used,
		})
	}
	if entries, err := os.ReadDir("/mnt"); err == nil {
		for _, e := range entries {
			if !e.IsDir() {
				continue
			}
			full := "/mnt/" + e.Name()
			label := e.Name()
			kind := "disk"
			if strings.HasPrefix(label, "raid-") || strings.HasPrefix(label, "raid_") {
				kind = "raid"
			}
			total, used, _ := stat(full)
			out = append(out, Mount{
				Path: full, Label: label, Type: kind, Total: total, Used: used,
			})
		}
	}
	return c.JSON(http.StatusOK, out)
}

func stat(p string) (total, used uint64, ok bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(p, &st); err != nil {
		return 0, 0, false
	}
	total = st.Blocks * uint64(st.Bsize)
	free := st.Bavail * uint64(st.Bsize)
	used = total - free
	return total, used, true
}
