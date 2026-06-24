package main

import (
	"net"
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
