package main

import (
	"encoding/binary"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

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
	}
	for s, want := range cases {
		if got := isInternal(net.ParseIP(s)); got != want {
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
