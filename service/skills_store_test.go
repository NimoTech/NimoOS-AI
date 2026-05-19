package service

import (
	"path/filepath"
	"testing"
)

func TestSkillsStore_BundlePath(t *testing.T) {
	s := &SkillsStore{Root: "/var/lib/nimoos/skills"}

	if got := s.BuiltinPath("photo-curator"); got != "/var/lib/nimoos/skills/builtin/photo-curator" {
		t.Fatalf("BuiltinPath: %s", got)
	}
	if got := s.UserPath("42", "my-skill"); got != "/var/lib/nimoos/skills/users/42/my-skill" {
		t.Fatalf("UserPath: %s", got)
	}
	if got := s.RuntimePath("42"); got != "/var/lib/nimoos/skills/.runtime/42" {
		t.Fatalf("RuntimePath: %s", got)
	}
}

func TestSkillsStore_RejectsBadID(t *testing.T) {
	s := &SkillsStore{Root: t.TempDir()}
	cases := []string{"", "..", "../etc", "with/slash", "with space", "with.dot",
		"abc-", "-abc", "123-"}
	for _, id := range cases {
		if err := ValidateSkillID(id); err == nil {
			t.Errorf("ValidateSkillID(%q) should fail", id)
		}
	}
	good := []string{"photo-curator", "abc", "x123", "doc-summarizer-2", "123-skill"}
	for _, id := range good {
		if err := ValidateSkillID(id); err != nil {
			t.Errorf("ValidateSkillID(%q) unexpected: %v", id, err)
		}
	}
	_ = filepath.Join // keep import
	_ = s             // keep s used
}
