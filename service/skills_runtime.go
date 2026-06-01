package service

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
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
//  2. Create new symlink at .runtime/<uid>.tmp-<N> → <uid>.v<N+1>
//  3. os.Rename(.runtime/<uid>.tmp-<N>, .runtime/<uid>) — atomic swap
//  4. Best-effort remove old <uid>.v<N>/ and any leftover .v* / .tmp-*
//
// `uninstalled` is the set of built-in skill IDs marked uninstalled for this user.
// `disabled` is the set of skill IDs (built-in or user) the user has paused;
// they remain installed but are hidden from the agent's runtime view so the
// LLM does not see or load them.
func RebuildRuntimeView(s *SkillsStore, userID string, uninstalled, disabled map[string]bool) error {
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
	// If we bail before the atomic swap, the half-built dir is orphan
	// garbage. Track success and remove it on failure.
	swapped := false
	defer func() {
		if !swapped {
			_ = os.RemoveAll(verDir)
		}
	}()

	builtIns, err := s.ListBuiltin()
	if err != nil {
		return err
	}
	for _, m := range builtIns {
		if uninstalled[m.ID] || disabled[m.ID] {
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
		if disabled[m.ID] {
			continue
		}
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
	if err := os.Rename(tmpLink, rt); err != nil {
		_ = os.Remove(tmpLink)
		return err
	}
	swapped = true

	// Sweep stale leftovers: prior versioned dirs (incl. the one this
	// rebuild replaced) and any orphaned .tmp-<ts> symlinks from crashed
	// rebuilds. The current verDir is preserved.
	sweepStaleRuntimeArtifacts(rtDir, userID, verDir)
	return nil
}

// sweepStaleRuntimeArtifacts removes any "<uid>.v*" directories and
// "<uid>.tmp-*" symlinks in rtDir except keepDir. Best-effort; called
// after a successful swap so we never delete the live target.
func sweepStaleRuntimeArtifacts(rtDir, userID, keepDir string) {
	entries, err := os.ReadDir(rtDir)
	if err != nil {
		return
	}
	verPrefix := userID + ".v"
	tmpPrefix := userID + ".tmp-"
	for _, e := range entries {
		name := e.Name()
		full := filepath.Join(rtDir, name)
		switch {
		case strings.HasPrefix(name, verPrefix):
			if full == keepDir {
				continue
			}
			_ = os.RemoveAll(full)
		case strings.HasPrefix(name, tmpPrefix):
			// Orphaned tmp symlinks from a crashed swap.
			_ = os.Remove(full)
		}
	}
}
