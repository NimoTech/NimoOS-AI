package main

import (
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
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
