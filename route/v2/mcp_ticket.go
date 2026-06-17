package v2

import (
	"crypto/rand"
	"encoding/hex"
	"sync"
	"time"
)

type ticketEntry struct {
	userID  string
	expires time.Time
}

// TicketStore mints one-time, short-lived tickets binding a run to a user, so
// the loopback /_internal/mcp/runtime endpoint can return that user's decrypted
// MCP config without trusting an arbitrary X-User-Id header.
type TicketStore struct {
	mu  sync.Mutex
	ttl time.Duration
	m   map[string]ticketEntry
}

func NewTicketStore(ttl time.Duration) *TicketStore {
	return &TicketStore{ttl: ttl, m: make(map[string]ticketEntry)}
}

func (s *TicketStore) Mint(userID string) string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	tok := hex.EncodeToString(b)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.m[tok] = ticketEntry{userID: userID, expires: time.Now().Add(s.ttl)}
	return tok
}

// Resolve returns the bound userID and consumes the ticket (one-time). Returns
// ok=false for unknown, already-consumed, or expired tickets.
func (s *TicketStore) Resolve(tok string) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.m[tok]
	if !ok {
		return "", false
	}
	delete(s.m, tok) // one-time regardless of expiry outcome
	if time.Now().After(e.expires) {
		return "", false
	}
	return e.userID, true
}
