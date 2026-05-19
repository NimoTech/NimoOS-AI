package service

import (
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// RebuildRuntimeView atomically rebuilds the per-user skill view.
//
// Why this is complicated: bwrap mounts <rt> as read-only into every
// agent run's sandbox. If we naively "rm -rf rt; mv tmp rt", a concurrent
// request hitting that gap mount-fails with ENOENT. Even a two-rename
// sequence has the same gap.
//
// True atomicity: <rt> is itself a symlink. `rename(2)` on a symlink is a
// single atomic syscall — readers either see the OLD target or the NEW
// target, never an in-between. Pattern:
//
//  1. Build new directory at .runtime/<uid>.v<N+1>/
//  2. Create new symlink at .runtime/<uid>.tmp → <uid>.v<N+1>
//  3. os.Rename(.runtime/<uid>.tmp, .runtime/<uid>) — atomic swap
//  4. Best-effort remove old <uid>.v<N>/ (next call cleans it up)
//
// `uninstalled` is the set of built-in skill IDs marked uninstalled for this user.
func RebuildRuntimeView(s *SkillsStore, userID string, uninstalled map[string]bool) error {
	rtDir := filepath.Dir(s.RuntimePath(userID))
	if err := os.MkdirAll(rtDir, 0o755); err != nil {
		return err
	}

	// Generate a unique versioned dir name. Time-based suffix is enough —
	// rebuilds for the same user are serialised by the caller's lock.
	verDir := filepath.Join(rtDir, userID+".v"+fmt.Sprintf("%d", time.Now().UnixNano()))
	if err := os.MkdirAll(verDir, 0o755); err != nil {
		return err
	}

	builtIns, err := s.ListBuiltin()
	if err != nil {
		return err
	}
	for _, m := range builtIns {
		if uninstalled[m.ID] {
			continue
		}
		target := s.BuiltinPath(m.ID)
		if err := os.Symlink(target, filepath.Join(verDir, m.ID)); err != nil {
			return fmt.Errorf("symlink builtin %s: %w", m.ID, err)
		}
	}
	userSkills, err := s.ListUser(userID)
	if err != nil {
		return err
	}
	for _, m := range userSkills {
		target := s.UserPath(userID, m.ID)
		if err := os.Symlink(target, filepath.Join(verDir, m.ID)); err != nil {
			return fmt.Errorf("symlink user %s: %w", m.ID, err)
		}
	}

	// Atomic symlink swap.
	rt := s.RuntimePath(userID)
	tmpLink := rt + ".tmp-" + fmt.Sprintf("%d", time.Now().UnixNano())
	if err := os.Symlink(verDir, tmpLink); err != nil {
		return err
	}
	// Capture the old target so we can clean it up after the swap.
	var oldTarget string
	if t, err := os.Readlink(rt); err == nil {
		oldTarget = t
		if !filepath.IsAbs(oldTarget) {
			oldTarget = filepath.Join(rtDir, oldTarget)
		}
	}
	if err := os.Rename(tmpLink, rt); err != nil {
		_ = os.Remove(tmpLink)
		return err
	}
	// Best-effort cleanup of the previous version.
	if oldTarget != "" && oldTarget != verDir {
		_ = os.RemoveAll(oldTarget)
	}
	return nil
}
