package service

import "time"

// backoffTable defines the circuit-breaker backoff duration for each consecutive failure streak.
// The first three entries are all 60s to tolerate brief network blips without immediately
// putting the server into a long cooldown; from the fourth entry onwards, we recognize
// sustained failure and escalate rapidly to a 2h cap, so that broken servers stop
// charging a timeout tax to every conversation turn.
var backoffTable = []time.Duration{
	60 * time.Second,
	60 * time.Second,
	60 * time.Second,
	5 * time.Minute,
	10 * time.Minute,
	30 * time.Minute,
	2 * time.Hour,
}

// Backoff returns the cooldown duration after failStreak consecutive probe failures.
// failStreak starts at 1. Non-positive streaks clamp to the first entry (60s).
// Streaks beyond the table length cap at the highest entry (2h).
func Backoff(failStreak int) time.Duration {
	if failStreak < 1 {
		failStreak = 1
	}
	if failStreak > len(backoffTable) {
		return backoffTable[len(backoffTable)-1]
	}
	return backoffTable[failStreak-1]
}
