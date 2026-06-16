package v2

import (
	"testing"
	"time"
)

func TestTicketStore_MintResolveOnce(t *testing.T) {
	ts := NewTicketStore(time.Minute)
	tok := ts.Mint("u1")
	if tok == "" {
		t.Fatal("empty ticket")
	}
	uid, ok := ts.Resolve(tok)
	if !ok || uid != "u1" {
		t.Fatalf("resolve failed: %q %v", uid, ok)
	}
	if _, ok := ts.Resolve(tok); ok {
		t.Fatal("ticket should be consumed after first resolve")
	}
}

func TestTicketStore_Expiry(t *testing.T) {
	ts := NewTicketStore(time.Duration(0)) // already-expired on mint
	tok := ts.Mint("u1")
	if _, ok := ts.Resolve(tok); ok {
		t.Fatal("expired ticket must not resolve")
	}
}

func TestTicketStore_UnknownTicket(t *testing.T) {
	ts := NewTicketStore(time.Minute)
	if _, ok := ts.Resolve("nope"); ok {
		t.Fatal("unknown ticket must not resolve")
	}
}
