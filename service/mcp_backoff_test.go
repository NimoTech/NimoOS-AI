package service

import (
	"testing"
	"time"
)

func TestBackoffTable(t *testing.T) {
	// The first three entries are all 60s to tolerate brief network blips without immediately
	// putting the server into a long cooldown; from the fourth entry onwards, we recognize
	// sustained failure and escalate rapidly to a 2h cap, so that broken servers stop
	// charging a timeout tax to every conversation turn.
	want := map[int]time.Duration{
		1: 60 * time.Second,
		2: 60 * time.Second,
		3: 60 * time.Second,
		4: 5 * time.Minute,
		5: 10 * time.Minute,
		6: 30 * time.Minute,
		7: 2 * time.Hour,
		8: 2 * time.Hour,
		99: 2 * time.Hour,
	}
	for streak, exp := range want {
		if got := Backoff(streak); got != exp {
			t.Fatalf("Backoff(%d) = %v, want %v", streak, got, exp)
		}
	}
}

func TestBackoffZeroAndNegativeClampToFirst(t *testing.T) {
	if Backoff(0) != 60*time.Second || Backoff(-3) != 60*time.Second {
		t.Fatal("non-positive streak must clamp to the first entry")
	}
}
