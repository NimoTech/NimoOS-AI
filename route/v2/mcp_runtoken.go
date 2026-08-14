package v2

import (
	"crypto/rand"
	"encoding/hex"
	"sync"
	"time"
)

type runTokenEntry struct {
	userID    string
	sessionID string
	expires   time.Time
}

// RunTokenStore and TicketStore are two separate things; do not merge them:
//
//	TicketStore — one-time, 30s, used for bootstrapping MCP config fetch at run start.
//	RunTokenStore — multi-use, 24h, used for Python to write back user approval mid-run.
//
// Why this is needed: the one-time ticket is consumed at run startup by the Runtime GET,
// while the user clicking "don't ask again" happens mid-run, at which point Python holds
// no credentials.
//
// Why TTL is 24h: a run can hang for a long time — the confirmation card's timeout upper
// limit is 24 hours (see agent/mcp_client/client.py:152-153). A shorter TTL would cause
// a "don't ask again" click three hours later to degrade silently, making the button look
// broken. The replay window is bounded by Release back to the run's actual duration;
// 24h is only the backstop for abnormal exit.
type RunTokenStore struct {
	mu  sync.Mutex
	ttl time.Duration
	m   map[string]runTokenEntry
}

func NewRunTokenStore(ttl time.Duration) *RunTokenStore {
	return &RunTokenStore{ttl: ttl, m: make(map[string]runTokenEntry)}
}

func (s *RunTokenStore) Mint(userID, sessionID string) string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	tok := hex.EncodeToString(b)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.m[tok] = runTokenEntry{userID: userID, sessionID: sessionID, expires: time.Now().Add(s.ttl)}
	return tok
}

// Resolve returns the bound userID and sessionID without consuming the token (multi-use).
// Returns ok=false for unknown or expired tokens.
func (s *RunTokenStore) Resolve(tok string) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.m[tok]
	if !ok {
		return "", false
	}
	if time.Now().After(e.expires) {
		return "", false
	}
	return e.userID, true
}

// Release invalidates the token immediately (regardless of expiry).
func (s *RunTokenStore) Release(tok string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.m, tok)
}
