package v2

import (
	"testing"
	"time"
)

func TestRunTokenIsMultiUse(t *testing.T) {
	// Unlike one-time tickets (mcp_ticket.go:47 unconditional delete),
	// run tokens are consumed mid-run by Python writeback, when the
	// one-time ticket has long since been consumed by Runtime GET.
	s := NewRunTokenStore(time.Hour)
	tok := s.Mint("u1", "sess1")
	for i := 0; i < 3; i++ {
		uid, ok := s.Resolve(tok)
		if !ok || uid != "u1" {
			t.Fatalf("resolve #%d failed — run token MUST be multi-use", i)
		}
	}
}

func TestRunTokenReleaseInvalidates(t *testing.T) {
	s := NewRunTokenStore(time.Hour)
	tok := s.Mint("u1", "sess1")
	s.Release(tok)
	if _, ok := s.Resolve(tok); ok {
		t.Fatal("released token must not resolve")
	}
}

func TestRunTokenExpires(t *testing.T) {
	s := NewRunTokenStore(0)
	tok := s.Mint("u1", "sess1")
	if _, ok := s.Resolve(tok); ok {
		t.Fatal("expired token must not resolve")
	}
}
