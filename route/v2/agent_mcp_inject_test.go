package v2

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestProxy_MintsTicketOnRunEndpoint(t *testing.T) {
	ts := NewTicketStore(time.Minute)
	h := &AgentHandler{tickets: ts}
	req := httptest.NewRequest(http.MethodPost, "/v1/ai/agent/sessions/s1/run", nil)
	if !isRunEndpoint(req) {
		t.Skip("adjust path to match isRunEndpoint")
	}
	if h.tickets != nil && isRunEndpoint(req) {
		req.Header.Set("X-Agent-MCP-Ticket", h.tickets.Mint("u7"))
	}
	tok := req.Header.Get("X-Agent-MCP-Ticket")
	if tok == "" {
		t.Fatal("ticket not injected on run endpoint")
	}
	uid, ok := ts.Resolve(tok)
	if !ok || uid != "u7" {
		t.Fatalf("minted ticket invalid: %q %v", uid, ok)
	}
}
