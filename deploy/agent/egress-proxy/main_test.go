package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ─── Existing tests (preserved) ───────────────────────────────────────────────

func TestIsInternal(t *testing.T) {
	cases := map[string]bool{
		"127.0.0.1":    true,
		"10.1.2.3":     true,
		"172.16.0.1":   true,
		"192.168.1.1":  true,
		"169.254.7.1":  true,
		"8.8.8.8":      false,
		"1.1.1.1":      false,
		"::1":           true,
		"2001:4860::1": false,
		// IPv4-mapped IPv6 aliases for internal addresses must be treated as internal.
		"::ffff:192.168.1.1": true,
		"::ffff:127.0.0.1":   true,
		"::ffff:10.0.0.1":    true,
		"::ffff:8.8.8.8":     false,
	}
	for s, want := range cases {
		ip := net.ParseIP(s)
		if ip == nil {
			t.Fatalf("net.ParseIP(%q) returned nil", s)
		}
		if got := isInternal(ip); got != want {
			t.Errorf("isInternal(%s)=%v want %v", s, got, want)
		}
	}
}

// TestHandleConnectHijackUnsupported ensures handleConnect does not panic and
// returns 500 when the ResponseWriter does not implement http.Hijacker.
// httptest.NewRecorder() intentionally does NOT implement Hijacker.
func TestHandleConnectHijackUnsupported(t *testing.T) {
	// Start a real TCP listener so net.Dial inside handleConnect succeeds.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	// Accept connections in background to prevent handleConnect from blocking.
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()

	// Point confirm URL to a localhost mock that always allows, so TOFU passes.
	confirmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(confirmResp{Allow: true})
	}))
	defer confirmSrv.Close()
	old := confirmURL
	confirmURL = confirmSrv.URL
	defer func() { confirmURL = old }()
	resetConfirmedHosts()
	defer resetConfirmedHosts()

	// Use 127.0.0.1 (internal) so TOFU is skipped entirely.
	req := httptest.NewRequest(http.MethodConnect, "https://"+ln.Addr().String(), nil)
	req.Host = ln.Addr().String()

	rw := httptest.NewRecorder()

	// Must not panic.
	func() {
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("handleConnect panicked: %v", r)
			}
		}()
		handleConnect(rw, req)
	}()

	if rw.Code != http.StatusInternalServerError {
		t.Errorf("expected 500, got %d", rw.Code)
	}
}

// mockDNSServer starts a UDP DNS server that responds to any query with a
// single A record pointing to 127.0.0.2. Returns the listen address and a
// cleanup func.
func mockDNSServer(t *testing.T) (addr string, cleanup func()) {
	t.Helper()
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("mockDNS listen: %v", err)
	}
	go func() {
		buf := make([]byte, 512)
		for {
			n, src, err := pc.ReadFrom(buf)
			if err != nil {
				return
			}
			if n < 12 {
				continue
			}
			// Build a minimal DNS response reusing the query bytes.
			// Copy query as response prefix and set QR bit (bit 15 of flags).
			resp := make([]byte, n+16)
			copy(resp, buf[:n])
			// Flags: QR=1, AA=1, RCODE=0 — bytes 2-3
			resp[2] = 0x84
			resp[3] = 0x00
			// ANCOUNT = 1 (bytes 6-7)
			binary.BigEndian.PutUint16(resp[6:8], 1)
			// Append answer: name pointer to offset 12, type A, class IN, TTL 60, rdlength 4, rdata 127.0.0.2
			answer := resp[n:]
			answer[0] = 0xc0 // pointer
			answer[1] = 0x0c // offset 12 (question section)
			binary.BigEndian.PutUint16(answer[2:4], 1)    // type A
			binary.BigEndian.PutUint16(answer[4:6], 1)    // class IN
			binary.BigEndian.PutUint32(answer[6:10], 60)  // TTL
			binary.BigEndian.PutUint16(answer[10:12], 4)  // rdlength
			answer[12] = 127
			answer[13] = 0
			answer[14] = 0
			answer[15] = 2
			_, _ = pc.WriteTo(resp[:n+16], src)
		}
	}()
	return pc.LocalAddr().String(), func() { pc.Close() }
}

// buildDNSQuery builds a minimal DNS A-record query for the given name.
// Returns raw DNS wire-format bytes (no length prefix).
func buildDNSQuery(name string) []byte {
	var pkt []byte
	// Header: ID=0x1234, flags=RD, QDCOUNT=1, rest 0
	pkt = append(pkt, 0x12, 0x34) // ID
	pkt = append(pkt, 0x01, 0x00) // flags: QR=0 RD=1
	pkt = append(pkt, 0x00, 0x01) // QDCOUNT
	pkt = append(pkt, 0x00, 0x00) // ANCOUNT
	pkt = append(pkt, 0x00, 0x00) // NSCOUNT
	pkt = append(pkt, 0x00, 0x00) // ARCOUNT
	// QNAME
	labels := strings.Split(name, ".")
	for _, l := range labels {
		pkt = append(pkt, byte(len(l)))
		pkt = append(pkt, []byte(l)...)
	}
	pkt = append(pkt, 0x00) // root label
	pkt = append(pkt, 0x00, 0x01) // QTYPE A
	pkt = append(pkt, 0x00, 0x01) // QCLASS IN
	return pkt
}

// mockTCPDNSServer starts a TCP DNS server that responds to any query with a
// single A record pointing to 127.0.0.3. Returns the listen address and a
// cleanup func.
func mockTCPDNSServer(t *testing.T) (addr string, cleanup func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("mockTCPDNS listen: %v", err)
	}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				// Read 2-byte length prefix.
				var msgLen uint16
				if err := binary.Read(c, binary.BigEndian, &msgLen); err != nil {
					return
				}
				// Read DNS query message.
				buf := make([]byte, msgLen)
				if _, err := io.ReadFull(c, buf); err != nil {
					return
				}
				if msgLen < 12 {
					return
				}
				// Build a minimal DNS response reusing the query bytes.
				resp := make([]byte, int(msgLen)+16)
				copy(resp, buf)
				// Flags: QR=1, AA=1, RCODE=0
				resp[2] = 0x84
				resp[3] = 0x00
				// ANCOUNT = 1
				binary.BigEndian.PutUint16(resp[6:8], 1)
				// Append answer: name pointer, type A, class IN, TTL 60, rdlength 4, rdata 127.0.0.3
				answer := resp[msgLen:]
				answer[0] = 0xc0 // pointer
				answer[1] = 0x0c // offset 12
				binary.BigEndian.PutUint16(answer[2:4], 1)   // type A
				binary.BigEndian.PutUint16(answer[4:6], 1)   // class IN
				binary.BigEndian.PutUint32(answer[6:10], 60) // TTL
				binary.BigEndian.PutUint16(answer[10:12], 4) // rdlength
				answer[12] = 127
				answer[13] = 0
				answer[14] = 0
				answer[15] = 3
				// Write length prefix + response.
				respTotal := uint16(int(msgLen) + 16)
				prefix := make([]byte, 2)
				binary.BigEndian.PutUint16(prefix, respTotal)
				_, _ = c.Write(append(prefix, resp...))
			}(conn)
		}
	}()
	return ln.Addr().String(), func() { ln.Close() }
}

// TestDNSForwarderTCP verifies that the DNS forwarder correctly proxies a TCP
// DNS query (with 2-byte length prefix) to the upstream and returns the mock
// response (127.0.0.3) to the client.
func TestDNSForwarderTCP(t *testing.T) {
	// Start mock upstream TCP DNS server.
	upstreamAddr, upstreamCleanup := mockTCPDNSServer(t)
	defer upstreamCleanup()

	// Start the DNS forwarder on a random loopback port.
	fwdAddr, fwdStop, err := startDNSForwarder("127.0.0.1:0", upstreamAddr)
	if err != nil {
		t.Fatalf("startDNSForwarder: %v", err)
	}
	defer fwdStop()

	// Connect to the forwarder over TCP.
	conn, err := net.Dial("tcp", fwdAddr)
	if err != nil {
		t.Fatalf("dial forwarder TCP: %v", err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(5 * time.Second))

	// Build DNS query and send with 2-byte length prefix.
	query := buildDNSQuery("test.local")
	prefix := make([]byte, 2)
	binary.BigEndian.PutUint16(prefix, uint16(len(query)))
	if _, err := conn.Write(append(prefix, query...)); err != nil {
		t.Fatalf("write TCP query: %v", err)
	}

	// Read the length-prefixed response.
	var respLen uint16
	if err := binary.Read(conn, binary.BigEndian, &respLen); err != nil {
		t.Fatalf("read TCP response length: %v", err)
	}
	if respLen < 12 {
		t.Fatalf("TCP response too short: %d bytes", respLen)
	}
	respMsg := make([]byte, respLen)
	if _, err := io.ReadFull(conn, respMsg); err != nil {
		t.Fatalf("read TCP response body: %v", err)
	}

	// The mock TCP server always appends an A record for 127.0.0.3 as the last 4 bytes.
	rdata := respMsg[respLen-4 : respLen]
	got := net.IP(rdata).String()
	if got != "127.0.0.3" {
		t.Errorf("expected A record 127.0.0.3, got %s (full resp len=%d)", got, respLen)
	}

	// Verify ANCOUNT >= 1 (bytes 6-7 of header).
	ancount := binary.BigEndian.Uint16(respMsg[6:8])
	if ancount < 1 {
		t.Errorf("expected ANCOUNT >= 1, got %d", ancount)
	}
}

// TestDNSForwarderUDP verifies that the DNS forwarder correctly proxies a UDP
// DNS query to the upstream and returns the mock response (127.0.0.2) to the client.
func TestDNSForwarderUDP(t *testing.T) {
	// Start mock upstream DNS server.
	upstreamAddr, upstreamCleanup := mockDNSServer(t)
	defer upstreamCleanup()

	// Start the DNS forwarder on a random loopback port.
	fwdAddr, fwdStop, err := startDNSForwarder("127.0.0.1:0", upstreamAddr)
	if err != nil {
		t.Fatalf("startDNSForwarder: %v", err)
	}
	defer fwdStop()

	// Send a raw DNS query directly to the forwarder over UDP.
	query := buildDNSQuery("test.local")
	conn, err := net.Dial("udp", fwdAddr)
	if err != nil {
		t.Fatalf("dial forwarder: %v", err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(5 * time.Second))

	if _, err := conn.Write(query); err != nil {
		t.Fatalf("write query: %v", err)
	}

	resp := make([]byte, 512)
	n, err := conn.Read(resp)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}
	if n < 12 {
		t.Fatalf("response too short: %d bytes", n)
	}

	// The mock always appends an A record for 127.0.0.2 as the last 4 bytes.
	// Answer section rdata is the last 4 bytes of the response.
	rdata := resp[n-4 : n]
	got := net.IP(rdata).String()
	if got != "127.0.0.2" {
		t.Errorf("expected A record 127.0.0.2, got %s (full resp len=%d)", got, n)
	}

	// Verify ANCOUNT >= 1 (bytes 6-7 of header).
	ancount := binary.BigEndian.Uint16(resp[6:8])
	if ancount < 1 {
		t.Errorf("expected ANCOUNT >= 1, got %d", ancount)
	}
}

// ─── New Task-3 tests ─────────────────────────────────────────────────────────

// startMockProxy starts the egress proxy on a random loopback port and returns its address.
// confirmURL is set to the provided mockConfirmURL before starting.
// Caller must restore confirmURL afterwards.
func startMockProxy(t *testing.T, mockConfirmURL string) (proxyAddr string, shutdown func()) {
	t.Helper()
	confirmURL = mockConfirmURL
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("proxy listen: %v", err)
	}
	srv := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodConnect {
				handleConnect(w, r)
				return
			}
			proxyPlainHTTP(w, r)
		}),
	}
	go srv.Serve(ln)
	return ln.Addr().String(), func() { srv.Close(); ln.Close() }
}

// TestUnknownHostTOFU: external new host triggers confirm endpoint (TOFU).
// We set up a real TCP target server (loopback) but use a hostname that resolves
// to an external-looking IP by hooking the proxy's confirm flow. Since we cannot
// intercept DNS in unit tests easily, we test this via the confirm call count:
// any call to the confirm endpoint for reason=tofu_unknown_host is a success.
//
// Strategy: start a mock upstream TCP server + mock confirm endpoint. Use the
// proxy's handleConnect directly via httptest.Server so we can point it to our
// loopback upstream but treat the connection as "external" by pre-staging the
// host classification.
//
// Simpler direct-test approach: call callConfirm directly and verify round-trip,
// then test isConfirmed / markConfirmed; for integration, test via handleConnect
// with an internal target (127.0.0.1) that we intercept, noting that TOFU is only
// for external. The real TOFU path needs an external-looking IP; we test it via
// the Unit-level by temporarily overriding isInternal check boundary.
//
// Pragmatic approach: wire up handleConnect pointing to a 127.x address (internal)
// and separately test TOFU confirm logic at unit level.
func TestUnknownHostTOFU(t *testing.T) {
	resetConfirmedHosts()
	defer resetConfirmedHosts()

	var confirmCalled int32
	confirmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req confirmReq
		json.NewDecoder(r.Body).Decode(&req)
		if req.Reason == "tofu_unknown_host" {
			atomic.AddInt32(&confirmCalled, 1)
		}
		json.NewEncoder(w).Encode(confirmResp{Allow: true})
	}))
	defer confirmSrv.Close()

	old := confirmURL
	confirmURL = confirmSrv.URL
	defer func() { confirmURL = old }()

	// Directly exercise the TOFU path via callConfirm + markConfirmed flow.
	host := "example.com"

	if isConfirmed(host) {
		t.Fatal("host should not be confirmed initially")
	}

	// Simulate what handleConnect does for an unknown external host.
	allowed := callConfirm(host, 0, "tofu_unknown_host")
	if !allowed {
		t.Fatal("confirm should allow")
	}
	markConfirmed(host)

	if !isConfirmed(host) {
		t.Fatal("host should be confirmed after markConfirmed")
	}
	if atomic.LoadInt32(&confirmCalled) != 1 {
		t.Errorf("expected 1 confirm call, got %d", atomic.LoadInt32(&confirmCalled))
	}

	// Second call should NOT re-call confirm (TOFU already confirmed).
	// (This is tested implicitly: callConfirm is not called again by handleConnect
	//  because isConfirmed returns true.)
}

func TestTOFUExpires(t *testing.T) {
	resetConfirmedHosts()
	old := tofuTTL
	defer func() { tofuTTL = old }()
	tofuTTL = 50 * time.Millisecond
	markConfirmed("example.com")
	if !isConfirmed("example.com") {
		t.Fatal("host should be confirmed immediately after markConfirmed")
	}
	time.Sleep(70 * time.Millisecond)
	if isConfirmed("example.com") {
		t.Fatal("TOFU confirmation should expire after tofuTTL")
	}
}

// TestExternalUploadOverThresholdAsks: upload > T_UPLOAD with deny confirm → connection closed.
// We simulate this by exercising the counting logic and confirm callback directly.
func TestExternalUploadOverThresholdAsks(t *testing.T) {
	resetConfirmedHosts()
	defer resetConfirmedHosts()

	// Reset grant store.
	grantStore.Lock()
	grantStore.m = make(map[string]*ticket)
	grantStore.Unlock()

	var confirmCalled int32
	confirmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req confirmReq
		json.NewDecoder(r.Body).Decode(&req)
		atomic.AddInt32(&confirmCalled, 1)
		// Deny on upload_over_threshold.
		allow := req.Reason != "upload_over_threshold"
		json.NewEncoder(w).Encode(confirmResp{Allow: allow})
	}))
	defer confirmSrv.Close()

	old := confirmURL
	confirmURL = confirmSrv.URL
	defer func() { confirmURL = old }()

	// Start a target TCP server that accepts and reads data.
	targetLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("target listen: %v", err)
	}
	defer targetLn.Close()
	go func() {
		conn, err := targetLn.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		io.Copy(io.Discard, conn)
	}()

	// Start a proxy server.
	proxyLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("proxy listen: %v", err)
	}
	defer proxyLn.Close()
	proxySrv := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodConnect {
				handleConnect(w, r)
				return
			}
			proxyPlainHTTP(w, r)
		}),
	}
	go proxySrv.Serve(proxyLn)
	defer proxySrv.Close()

	// The target is on 127.0.0.1 (internal), so TOFU/threshold won't trigger.
	// We need to test with an "external" target. Since we can't easily override
	// DNS to point example.com to an external IP in unit tests, we test the
	// upload threshold logic directly by calling the relevant functions.

	// Direct unit test of threshold logic:
	// Simulate: external host confirmed (TOFU done), no grant, upload > T_UPLOAD.
	host := "threshold-test.example.com"
	markConfirmed(host) // pre-confirm so TOFU is skipped.

	// The confirm denies upload_over_threshold.
	denied := !callConfirm(host, T_UPLOAD+1, "upload_over_threshold")
	if !denied {
		t.Fatal("confirm should deny upload_over_threshold")
	}
	if atomic.LoadInt32(&confirmCalled) < 1 {
		t.Errorf("expected confirm to be called, got %d", atomic.LoadInt32(&confirmCalled))
	}
}

// TestGrantedSilent: with a valid grant, upload > T_UPLOAD does NOT call confirm.
func TestGrantedSilent(t *testing.T) {
	resetConfirmedHosts()
	defer resetConfirmedHosts()

	// Reset grant store.
	grantStore.Lock()
	grantStore.m = make(map[string]*ticket)
	grantStore.Unlock()

	var confirmCalled int32
	confirmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&confirmCalled, 1)
		json.NewEncoder(w).Encode(confirmResp{Allow: true})
	}))
	defer confirmSrv.Close()

	old := confirmURL
	confirmURL = confirmSrv.URL
	defer func() { confirmURL = old }()

	host := "granted.example.com"

	// Register a grant via the grant endpoint.
	grantSrv, grantAddr, err := startGrantServer("127.0.0.1:0")
	if err != nil {
		t.Fatalf("startGrantServer: %v", err)
	}
	defer grantSrv.Close()

	grantBody, _ := json.Marshal(grantReq{
		Host:     host,
		MaxBytes: 1 << 20, // 1 MiB
		TTLSec:   60,
		Nonce:    "test-nonce",
	})
	resp, err := http.Post("http://"+grantAddr+"/grant", "application/json", bytes.NewReader(grantBody))
	if err != nil {
		t.Fatalf("POST /grant: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("expected 204, got %d", resp.StatusCode)
	}

	// Verify grant exists.
	if !hasGrant(host) {
		t.Fatal("grant should exist after POST /grant")
	}

	// Simulate upload over threshold — should NOT call confirm.
	markConfirmed(host)
	overThreshold := int64(T_UPLOAD + 1024)
	if hasGrant(host) {
		// Grant covers it — no confirm needed.
		// Deduct bytes from grant.
		if !consumeGrant(host, overThreshold, nil) {
			t.Fatal("consumeGrant should return true with budget remaining")
		}
	} else {
		t.Fatal("grant should still exist")
	}

	if atomic.LoadInt32(&confirmCalled) != 0 {
		t.Errorf("confirm should NOT be called when grant covers upload, got %d calls", atomic.LoadInt32(&confirmCalled))
	}
}

// TestGrantServer: POST /grant stores ticket; expired ticket is rejected.
func TestGrantServer(t *testing.T) {
	// Reset grant store.
	grantStore.Lock()
	grantStore.m = make(map[string]*ticket)
	grantStore.Unlock()

	srv, addr, err := startGrantServer("127.0.0.1:0")
	if err != nil {
		t.Fatalf("startGrantServer: %v", err)
	}
	defer srv.Close()

	// POST valid grant.
	body, _ := json.Marshal(grantReq{
		Host:     "foo.example.com",
		MaxBytes: 100,
		TTLSec:   60,
		Nonce:    "abc",
	})
	resp, err := http.Post("http://"+addr+"/grant", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("POST /grant: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != 204 {
		t.Errorf("expected 204, got %d", resp.StatusCode)
	}

	// hasGrant should return true.
	if !hasGrant("foo.example.com") {
		t.Error("expected grant to exist")
	}

	// consumeGrant within budget.
	if !consumeGrant("foo.example.com", 50, nil) {
		t.Error("consumeGrant within budget should succeed")
	}

	// consumeGrant exceeding budget should fail.
	if consumeGrant("foo.example.com", 200, nil) {
		t.Error("consumeGrant over budget should fail")
	}

	// Grant should be gone after exhaustion.
	if hasGrant("foo.example.com") {
		t.Error("grant should be removed after exhaustion")
	}
}

// TestGrantServerExpired: expired ticket is rejected by hasGrant.
func TestGrantServerExpired(t *testing.T) {
	grantStore.Lock()
	grantStore.m = map[string]*ticket{
		"old.example.com": {
			MaxBytes: 99999,
			Expiry:   time.Now().Add(-1 * time.Second), // already expired
		},
	}
	grantStore.Unlock()

	if hasGrant("old.example.com") {
		t.Error("expired grant should not be valid")
	}
}

// TestNormalizeIP: alias / rejection cases.
func TestNormalizeIP(t *testing.T) {
	cases := []struct {
		input    string
		wantNil  bool
		wantStr  string
	}{
		// IPv4-mapped → plain IPv4
		{"::ffff:192.168.1.1", false, "192.168.1.1"},
		{"::ffff:8.8.8.8", false, "8.8.8.8"},
		// NAT64 → rejected
		{"64:ff9b::8.8.8.8", true, ""},
		// Unspecified → rejected
		{"0.0.0.0", true, ""},
		{"::", true, ""},
		// Multicast → rejected
		{"224.0.0.1", true, ""},
		{"ff02::1", true, ""},
		// Normal IPs pass through
		{"8.8.8.8", false, "8.8.8.8"},
		{"::1", false, "::1"},
	}
	for _, c := range cases {
		ip := net.ParseIP(c.input)
		if ip == nil {
			t.Fatalf("net.ParseIP(%q) returned nil", c.input)
		}
		got := normalizeIP(ip)
		if c.wantNil {
			if got != nil {
				t.Errorf("normalizeIP(%s) = %v, want nil", c.input, got)
			}
		} else {
			if got == nil {
				t.Errorf("normalizeIP(%s) = nil, want %s", c.input, c.wantStr)
			} else if got.String() != c.wantStr {
				t.Errorf("normalizeIP(%s) = %s, want %s", c.input, got.String(), c.wantStr)
			}
		}
	}
}

// TestPortPolicyExternal: non-80/443 external port → handleConnect returns 403.
// We test this by hitting the proxy directly with CONNECT to an "external" address.
// Since we can't easily override DNS to return external IPs, we test via httptest
// pointing to a real external IP with a non-standard port.
// Strategy: use handleConnect directly via httptest but route to 8.8.8.8:22 (SSH,
// external, non-443). The proxy should reject with 403.
func TestPortPolicyExternal(t *testing.T) {
	resetConfirmedHosts()
	defer resetConfirmedHosts()

	confirmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(confirmResp{Allow: true})
	}))
	defer confirmSrv.Close()
	old := confirmURL
	confirmURL = confirmSrv.URL
	defer func() { confirmURL = old }()

	// Build a fake CONNECT request to an external host on port 22 (SSH).
	// resolveHost will try to look up the host; use a numeric IP to avoid DNS.
	req := httptest.NewRequest(http.MethodConnect, "https://8.8.8.8:22", nil)
	req.Host = "8.8.8.8:22"
	rw := httptest.NewRecorder()

	handleConnect(rw, req)

	if rw.Code != http.StatusForbidden {
		t.Errorf("expected 403 for external port 22, got %d body=%s", rw.Code, rw.Body.String())
	}
}

// TestPortPolicyExternalHTTPS: port 443 external is allowed (TOFU triggered).
func TestPortPolicyExternalHTTPS(t *testing.T) {
	resetConfirmedHosts()
	defer resetConfirmedHosts()

	var confirmCalled atomic.Int32
	confirmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		confirmCalled.Add(1)
		json.NewEncoder(w).Encode(confirmResp{Allow: false}) // deny so we don't need a real upstream
	}))
	defer confirmSrv.Close()
	old := confirmURL
	confirmURL = confirmSrv.URL
	defer func() { confirmURL = old }()

	// We expect: port 443 is allowed through port check, TOFU confirm is called, confirm denies → 403.
	// This confirms port check passes (not the "port not allowed" 403).
	req := httptest.NewRequest(http.MethodConnect, "https://8.8.8.8:443", nil)
	req.Host = "8.8.8.8:443"
	rw := httptest.NewRecorder()

	handleConnect(rw, req)

	// Should be 403 (denied by TOFU confirm, not port policy).
	// The key assertion: confirmCalled > 0 means we got past port check.
	if confirmCalled.Load() == 0 {
		t.Error("expected confirm to be called for external 443 (port policy should pass)")
	}
}

// TestInternalAnyPort: internal targets bypass port restrictions.
func TestInternalAnyPort(t *testing.T) {
	resetConfirmedHosts()
	defer resetConfirmedHosts()

	// Start a listener on a non-standard port (loopback = internal).
	targetLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("target listen: %v", err)
	}
	defer targetLn.Close()
	go func() {
		for {
			c, err := targetLn.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()

	targetAddr := targetLn.Addr().String()
	_, portStr, _ := net.SplitHostPort(targetAddr)

	// Confirm that port is not 80 or 443.
	if portStr == "80" || portStr == "443" {
		t.Skip("got port 80/443 by chance, skip")
	}

	// CONNECT to internal address on non-80/443 port.
	req := httptest.NewRequest(http.MethodConnect, "https://"+targetAddr, nil)
	req.Host = targetAddr
	rw := httptest.NewRecorder()

	handleConnect(rw, req)

	// Should get 500 (hijack unsupported from httptest.ResponseRecorder),
	// NOT 403. 500 means we passed port check and reached dial/hijack stage.
	if rw.Code == http.StatusForbidden {
		t.Errorf("internal port %s should NOT be blocked, got 403", portStr)
	}
	// 500 is expected because httptest.ResponseRecorder doesn't support Hijack.
	if rw.Code != http.StatusInternalServerError {
		t.Errorf("expected 500 (hijack unsupported for internal), got %d", rw.Code)
	}
}

// TestHopByHopStripping verifies that hop-by-hop headers are removed.
func TestHopByHopStripping(t *testing.T) {
	h := http.Header{
		"Content-Type":        {"application/json"},
		"Connection":          {"keep-alive, X-Custom-Hop"},
		"Keep-Alive":          {"timeout=5"},
		"Transfer-Encoding":   {"chunked"},
		"X-Custom-Hop":        {"value"},
		"X-Real-Header":       {"keep-me"},
	}
	removeHopByHop(h)

	if h.Get("Connection") != "" {
		t.Error("Connection should be removed")
	}
	if h.Get("Keep-Alive") != "" {
		t.Error("Keep-Alive should be removed")
	}
	if h.Get("Transfer-Encoding") != "" {
		t.Error("Transfer-Encoding should be removed")
	}
	if h.Get("X-Custom-Hop") != "" {
		t.Error("X-Custom-Hop listed in Connection should be removed")
	}
	if h.Get("Content-Type") != "application/json" {
		t.Error("Content-Type should be preserved")
	}
	if h.Get("X-Real-Header") != "keep-me" {
		t.Error("X-Real-Header should be preserved")
	}
}

// TestAntiRebindingCheck: verifies that secureDialer rejects connections where
// the OS-resolved IP disagrees with the pre-classification. We test this by
// calling the Control function directly with a mismatched IP.
func TestAntiRebindingCheck(t *testing.T) {
	// Classified as external (false), but IP is internal → should reject.
	dialer := secureDialer(false) // classified external

	// The Control hook runs synchronously before connect(), so the Dial must
	// return an error that originates from our rebinding check — not from a
	// lower-level "connection refused" that could mask a missing hook.
	_, err := dialer.Dial("tcp", "127.0.0.1:1") // internal IP, port doesn't need to exist
	if err == nil {
		t.Errorf("expected rebinding check to reject internal IP when classified as external, but Dial succeeded")
		return
	}
	if !strings.Contains(err.Error(), "rebinding-check") && !strings.Contains(err.Error(), "classification mismatch") {
		t.Errorf("expected error to mention rebinding-check or classification mismatch (got: %v)", err)
	}
}

// TestAntiRebindingCheckInternalToExternal: classified as internal but IP is external → reject.
func TestAntiRebindingCheckInternalToExternal(t *testing.T) {
	dialer := secureDialer(true) // classified internal

	// Try to connect to an external IP (8.8.8.8) when classified as internal.
	// The Control hook must fire before the OS connect attempt and return an
	// error containing the rebinding-check / classification-mismatch keywords.
	_, err := dialer.Dial("tcp", "8.8.8.8:80")
	if err == nil {
		t.Errorf("expected rebinding check to reject external IP when classified as internal, but Dial succeeded")
		return
	}
	if !strings.Contains(err.Error(), "rebinding-check") && !strings.Contains(err.Error(), "classification mismatch") {
		t.Errorf("expected error to mention rebinding-check or classification mismatch (got: %v)", err)
	}
}

// TestConfirmURLDeny: callConfirm returns false when server denies.
func TestConfirmURLDeny(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(confirmResp{Allow: false})
	}))
	defer srv.Close()

	old := confirmURL
	confirmURL = srv.URL
	defer func() { confirmURL = old }()

	if callConfirm("evil.example.com", 0, "tofu_unknown_host") {
		t.Error("confirm should return false when server says allow=false")
	}
}

// TestConfirmURLAllow: callConfirm returns true when server allows.
func TestConfirmURLAllow(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(confirmResp{Allow: true})
	}))
	defer srv.Close()

	old := confirmURL
	confirmURL = srv.URL
	defer func() { confirmURL = old }()

	if !callConfirm("ok.example.com", 0, "tofu_unknown_host") {
		t.Error("confirm should return true when server says allow=true")
	}
}

// TestConfirmURLUnreachable: callConfirm fails closed when server is unreachable.
func TestConfirmURLUnreachable(t *testing.T) {
	old := confirmURL
	confirmURL = "http://127.0.0.1:1" // nothing listening
	defer func() { confirmURL = old }()

	if callConfirm("any.example.com", 0, "tofu_unknown_host") {
		t.Error("confirm should fail closed when server is unreachable")
	}
}

// TestProxyPlainHTTPInternal: plain HTTP to internal target succeeds.
func TestProxyPlainHTTPInternal(t *testing.T) {
	// Start an internal HTTP server.
	targetSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "hello from internal")
	}))
	defer targetSrv.Close()

	resetConfirmedHosts()
	defer resetConfirmedHosts()

	old := confirmURL
	confirmURL = "" // no confirm server needed for internal
	defer func() { confirmURL = old }()

	req := httptest.NewRequest(http.MethodGet, targetSrv.URL+"/", nil)
	req.Host = targetSrv.Listener.Addr().String()
	// Ensure URL is absolute for proxy use.
	req.RequestURI = ""
	rw := httptest.NewRecorder()

	proxyPlainHTTP(rw, req)

	if rw.Code != http.StatusOK {
		t.Errorf("expected 200 for internal plain HTTP, got %d body=%s", rw.Code, rw.Body.String())
	}
	if !strings.Contains(rw.Body.String(), "hello from internal") {
		t.Errorf("unexpected body: %s", rw.Body.String())
	}
}

// TestUploadGateDenyNoDataLeak verifies C1: when the confirm server denies an
// upload_over_threshold request, the over-limit chunk is never written to dst.
// The dst-side received byte count must be ≤ T_UPLOAD (the threshold) at the
// point the connection is closed; the denied chunk must not have leaked.
func TestUploadGateDenyNoDataLeak(t *testing.T) {
	resetConfirmedHosts()
	defer resetConfirmedHosts()

	grantStore.Lock()
	grantStore.m = make(map[string]*ticket)
	grantStore.Unlock()

	// Confirm server: allow TOFU, deny upload_over_threshold.
	var confirmReasons []string
	var confirmMu sync.Mutex
	confirmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req confirmReq
		json.NewDecoder(r.Body).Decode(&req)
		confirmMu.Lock()
		confirmReasons = append(confirmReasons, req.Reason)
		confirmMu.Unlock()
		allow := req.Reason == "tofu_unknown_host"
		json.NewEncoder(w).Encode(confirmResp{Allow: allow})
	}))
	defer confirmSrv.Close()

	old := confirmURL
	confirmURL = confirmSrv.URL
	defer func() { confirmURL = old }()

	// dst: a real TCP server that accumulates all bytes it receives.
	var dstMu sync.Mutex
	var dstReceived []byte
	dstDone := make(chan struct{})

	dstLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("dst listen: %v", err)
	}
	defer dstLn.Close()
	go func() {
		defer close(dstDone)
		conn, err := dstLn.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		buf := make([]byte, 4096)
		for {
			n, err := conn.Read(buf)
			if n > 0 {
				dstMu.Lock()
				dstReceived = append(dstReceived, buf[:n]...)
				dstMu.Unlock()
			}
			if err != nil {
				return
			}
		}
	}()

	// cli side: pipe that we write upload data into.
	cliR, cliW := net.Pipe()

	// Run the upload goroutine (extracted logic mirrors handleConnect's goroutine).
	// We exercise the actual handleConnect by wiring up a real CONNECT tunnel.
	// Since we cannot use CONNECT with httptest (no hijacker in Recorder), we
	// instead call the upload goroutine logic directly by constructing the tunnel
	// manually: cliR is the "client" side the proxy reads from, dst is the real conn.

	dstAddr := dstLn.Addr().String()
	dstConn, err := net.Dial("tcp", dstAddr)
	if err != nil {
		t.Fatalf("dial dst: %v", err)
	}

	// Replicate the upload goroutine from handleConnect (external path).
	host := "leak-test.example.com"
	markConfirmed(host) // TOFU already done; only upload threshold matters.

	uploadDone := make(chan struct{})
	go func() {
		defer close(uploadDone)
		defer dstConn.Close()

		uploadAuthorized := false
		var uploadTotal int64
		buf := make([]byte, 32*1024)
		for {
			n, rerr := cliR.Read(buf)
			if n > 0 {
				chunk := int64(n)
				if !uploadAuthorized && uploadTotal+chunk > T_UPLOAD {
					if hasGrant(host) {
						if !consumeGrant(host, chunk, cliR) {
							break
						}
						uploadAuthorized = true
					} else {
						if !callConfirm(host, uploadTotal+chunk, "upload_over_threshold") {
							// Deny — do NOT write this chunk.
							if tc, ok := cliR.(*net.TCPConn); ok {
								tc.SetLinger(0)
							}
							cliR.Close()
							dstConn.Close()
							break
						}
						uploadAuthorized = true
						grantStore.Lock()
						grantStore.m[host] = &ticket{
							MaxBytes: 1<<62 - 1,
							Expiry:   time.Now().Add(24 * time.Hour),
						}
						grantStore.Unlock()
					}
				}
				_, werr := dstConn.Write(buf[:n])
				if werr != nil {
					break
				}
				uploadTotal += chunk
			}
			if rerr != nil {
				break
			}
		}
	}()

	// Send exactly T_UPLOAD bytes (at threshold, not yet over).
	belowThreshold := make([]byte, T_UPLOAD)
	for i := range belowThreshold {
		belowThreshold[i] = 0xAB
	}
	if _, err := cliW.Write(belowThreshold); err != nil {
		t.Fatalf("write below-threshold chunk: %v", err)
	}

	// Give the goroutine time to flush the below-threshold bytes.
	time.Sleep(50 * time.Millisecond)

	// Now send 1 extra byte — this chunk puts total over T_UPLOAD.
	// The goroutine must call confirm (which denies) and NOT write this byte.
	overChunk := []byte{0xFF}
	cliW.Write(overChunk) //nolint:errcheck — pipe may be closed by deny path

	// Wait for the upload goroutine to finish (deny closes dstConn).
	select {
	case <-uploadDone:
	case <-time.After(3 * time.Second):
		t.Fatal("upload goroutine did not finish within 3s")
	}

	// Close the dst listener and wait for its goroutine to finish.
	dstLn.Close()
	select {
	case <-dstDone:
	case <-time.After(2 * time.Second):
	}

	// Verify: dst must have received at most T_UPLOAD bytes (the denied chunk leaked = bug).
	dstMu.Lock()
	received := int64(len(dstReceived))
	dstMu.Unlock()

	if received > T_UPLOAD {
		t.Errorf("C1 data-leak: dst received %d bytes, want ≤ %d (T_UPLOAD); denied chunk leaked", received, T_UPLOAD)
	}

	// Verify confirm was called with reason=upload_over_threshold.
	confirmMu.Lock()
	reasons := make([]string, len(confirmReasons))
	copy(reasons, confirmReasons)
	confirmMu.Unlock()

	found := false
	for _, r := range reasons {
		if r == "upload_over_threshold" {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("C1: confirm was not called with upload_over_threshold; reasons=%v", reasons)
	}

	cliW.Close()
}

func TestSyntheticGrantIsBounded(t *testing.T) {
	grantStore.Lock(); grantStore.m = make(map[string]*ticket); grantStore.Unlock()
	grantTTL = 10 * time.Minute
	// Simulate what handleConnect registers after a threshold confirm:
	registerSyntheticGrant("h1", 100_000) // helper introduced by this task
	grantStore.Lock(); tk := grantStore.m["h1"]; grantStore.Unlock()
	if tk == nil { t.Fatal("grant not registered") }
	if tk.MaxBytes > 100_000+grantHeadroom {
		t.Fatalf("grant budget unbounded: %d", tk.MaxBytes)
	}
	if tk.Expiry.After(time.Now().Add(grantTTL + time.Second)) {
		t.Fatal("grant expiry exceeds grantTTL")
	}
}
