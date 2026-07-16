package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"syscall"
	"time"
)

// ─── IP classification ───────────────────────────────────────────────────────

var internalV4 = []*net.IPNet{
	cidr("127.0.0.0/8"),
	cidr("10.0.0.0/8"),
	cidr("172.16.0.0/12"),
	cidr("192.168.0.0/16"),
	cidr("169.254.0.0/16"),
}
var internalV6 = []*net.IPNet{
	cidr("::1/128"),
	cidr("fc00::/7"),
	cidr("fe80::/10"),
}

var nat64Prefix = cidr("64:ff9b::/96")

func cidr(s string) *net.IPNet {
	_, n, err := net.ParseCIDR(s)
	if err != nil {
		panic(err)
	}
	return n
}

// normalizeIP converts IPv4-mapped IPv6 addresses (::ffff:x.x.x.x) to plain
// IPv4, and rejects NAT64 / unspecified / multicast addresses (returns nil for
// those so callers can treat them as "not internal" → blocked).
func normalizeIP(ip net.IP) net.IP {
	if ip == nil {
		return nil
	}
	// Reject unspecified
	if ip.Equal(net.IPv4zero) || ip.Equal(net.IPv6unspecified) {
		return nil
	}
	// Reject multicast
	if ip.IsMulticast() {
		return nil
	}
	// Reject NAT64 prefix 64:ff9b::/96
	if nat64Prefix.Contains(ip) {
		return nil
	}
	// Unwrap IPv4-mapped IPv6 (::ffff:x.x.x.x) → IPv4
	if v4 := ip.To4(); v4 != nil {
		return v4
	}
	return ip
}

// isInternal returns true if ip is a loopback / RFC-1918 / link-local / ULA address.
// It normalizes IPv4-mapped IPv6 first, so ::ffff:192.168.x.x is treated as internal.
func isInternal(ip net.IP) bool {
	ip = normalizeIP(ip)
	if ip == nil {
		return false
	}
	for _, n := range internalV4 {
		if n.Contains(ip) {
			return true
		}
	}
	for _, n := range internalV6 {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

var metadataIPs = []net.IP{
	net.ParseIP("169.254.169.254"), // AWS/GCP/Azure IMDS
	net.ParseIP("169.254.170.2"),   // ECS task metadata
	net.ParseIP("fd00:ec2::254"),   // IPv6 IMDS
}

// isMetadataIP reports whether ip is a cloud metadata endpoint. These are
// link-local (thus "internal") but must be DENIED — they are the classic
// SSRF credential-exfil target. The proxy's own plumbing (169.254.7.1) is
// NOT in this list.
func isMetadataIP(ip net.IP) bool {
	ip = normalizeIP(ip)
	if ip == nil {
		return false
	}
	for _, m := range metadataIPs {
		if ip.Equal(m) {
			return true
		}
	}
	return false
}

// ─── Hop-by-hop headers (RFC 7230 §6.1) ─────────────────────────────────────

var hopByHopHeaders = map[string]bool{
	"Connection":          true,
	"Keep-Alive":          true,
	"Proxy-Authenticate":  true,
	"Proxy-Authorization": true,
	"Te":                  true,
	"Trailers":            true,
	"Transfer-Encoding":   true,
	"Upgrade":             true,
	"Proxy-Connection":    true,
}

func removeHopByHop(h http.Header) {
	// Also strip headers listed in Connection:
	for _, c := range h["Connection"] {
		for _, tok := range strings.Split(c, ",") {
			h.Del(strings.TrimSpace(tok))
		}
	}
	for k := range hopByHopHeaders {
		h.Del(k)
	}
}

// ─── Global state ─────────────────────────────────────────────────────────────

var uploadThreshold int64 = 65536 // -upload-threshold; bytes before an external upload asks confirm

var confirmURL string // set from -confirm-url flag

// confirmedHosts is the TOFU allowlist (process-global). Each entry carries an
// expiry: an auto-remembered host must NOT be trusted forever — a single
// injection that wins one confirm would otherwise get a permanent silent
// egress channel. Expired entries require a fresh first-connection confirm.
var confirmedHosts struct {
	sync.Mutex
	m map[string]time.Time // host -> expiry
}

// tofuTTL is how long an auto-TOFU confirmation lasts. Configurable via
// -tofu-ttl; default 1h (short enough to bound a stolen confirm, long enough
// not to nag during a normal session).
var tofuTTL = time.Hour

func init() {
	confirmedHosts.m = make(map[string]time.Time)
}

func isConfirmed(host string) bool {
	confirmedHosts.Lock()
	defer confirmedHosts.Unlock()
	exp, ok := confirmedHosts.m[host]
	if !ok {
		return false
	}
	if time.Now().After(exp) {
		delete(confirmedHosts.m, host)
		return false
	}
	return true
}

func markConfirmed(host string) {
	confirmedHosts.Lock()
	defer confirmedHosts.Unlock()
	confirmedHosts.m[host] = time.Now().Add(tofuTTL)
}

func resetConfirmedHosts() {
	confirmedHosts.Lock()
	defer confirmedHosts.Unlock()
	confirmedHosts.m = make(map[string]time.Time)
}

// ─── Egress confirm callback ──────────────────────────────────────────────────

type confirmReq struct {
	Host   string `json:"host"`
	Bytes  int64  `json:"bytes"`
	Reason string `json:"reason"`
}

type confirmResp struct {
	Allow bool `json:"allow"`
}

// confirmClient is the HTTP client used for egress-confirm callbacks.
// A 5-second timeout ensures fail-closed behaviour on slow/hung control planes.
var confirmClient = &http.Client{Timeout: 5 * time.Second}

// callConfirm calls the confirm URL and returns whether the request is allowed.
// Returns false on any error (fail-closed): network errors, timeouts, non-200,
// or JSON decode failures all result in deny.
func callConfirm(host string, bytesCount int64, reason string) bool {
	if confirmURL == "" {
		return false
	}
	body, _ := json.Marshal(confirmReq{Host: host, Bytes: bytesCount, Reason: reason})
	resp, err := confirmClient.Post(confirmURL, "application/json", bytes.NewReader(body))
	if err != nil {
		log.Printf("egress-confirm: POST error: %v", err)
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.Printf("egress-confirm: non-200 status: %d", resp.StatusCode)
		return false
	}
	var cr confirmResp
	if err := json.NewDecoder(resp.Body).Decode(&cr); err != nil {
		log.Printf("egress-confirm: decode error: %v", err)
		return false
	}
	return cr.Allow
}

// ─── Grant tickets ────────────────────────────────────────────────────────────

type ticket struct {
	MaxBytes int64
	Expiry   time.Time
	Nonce    string
}

var grantStore struct {
	sync.Mutex
	m map[string]*ticket // key = host
}

func init() {
	grantStore.m = make(map[string]*ticket)
}

const grantHeadroom = 1 << 20 // 1 MiB headroom over the observed upload size

// grantTTL bounds how long a synthetic post-confirm grant stays valid.
// Configurable via -grant-ttl; default 10m (was a hardcoded 24h).
var grantTTL = 10 * time.Minute

// registerSyntheticGrant records a bounded grant after a user confirms an
// over-threshold upload: budget = observed bytes + headroom (NOT unlimited),
// expiry = grantTTL. Prevents "one confirm -> unlimited 24h egress".
func registerSyntheticGrant(host string, observedBytes int64) {
	grantStore.Lock()
	grantStore.m[host] = &ticket{
		MaxBytes: observedBytes + grantHeadroom,
		Expiry:   time.Now().Add(grantTTL),
	}
	grantStore.Unlock()
}

type grantReq struct {
	Host     string `json:"host"`
	MaxBytes int64  `json:"max_bytes"`
	TTLSec   int    `json:"ttl_sec"`
	Nonce    string `json:"nonce"`
}

// consumeGrant checks whether a valid grant exists for host and deducts n bytes.
// Returns true if the grant covers this usage (grant still has budget).
// If grant exists but budget drops below 0, it RSTs the connection (if conn non-nil)
// and returns false.
func consumeGrant(host string, n int64, conn net.Conn) bool {
	grantStore.Lock()
	defer grantStore.Unlock()
	t, ok := grantStore.m[host]
	if !ok {
		return false
	}
	if time.Now().After(t.Expiry) {
		delete(grantStore.m, host)
		return false
	}
	t.MaxBytes -= n
	if t.MaxBytes < 0 {
		delete(grantStore.m, host)
		if conn != nil {
			if tc, ok := conn.(*net.TCPConn); ok {
				tc.SetLinger(0) // RST
			}
			conn.Close()
		}
		return false
	}
	return true
}

// hasGrant returns true if a non-expired grant with remaining budget > 0 exists.
func hasGrant(host string) bool {
	grantStore.Lock()
	defer grantStore.Unlock()
	t, ok := grantStore.m[host]
	if !ok {
		return false
	}
	if time.Now().After(t.Expiry) {
		delete(grantStore.m, host)
		return false
	}
	return t.MaxBytes > 0
}

// ─── Secure dialer (anti-rebinding) ──────────────────────────────────────────

// secureDialer returns a *net.Dialer whose Control hook re-verifies the OS-resolved
// IP at connect time to prevent DNS rebinding attacks.
// classifiedInternal must match the classification made at request-parse time;
// the hook blocks if the true IP disagrees.
func secureDialer(classifiedInternal bool) *net.Dialer {
	return &net.Dialer{
		Timeout: 30 * time.Second,
		Control: func(network, address string, c syscall.RawConn) error {
			host, _, err := net.SplitHostPort(address)
			if err != nil {
				return fmt.Errorf("rebinding-check: split host: %w", err)
			}
			ip := net.ParseIP(host)
			if ip == nil {
				return fmt.Errorf("rebinding-check: non-IP address at dial time: %s", host)
			}
			ip = normalizeIP(ip)
			if ip == nil {
				return fmt.Errorf("rebinding-check: rejected IP %s (unspecified/multicast/NAT64)", host)
			}
			reallyInternal := isInternal(ip)
			if reallyInternal != classifiedInternal {
				return fmt.Errorf("rebinding-check: IP %s classification mismatch (pre=%v now=%v)", ip, classifiedInternal, reallyInternal)
			}
			return nil
		},
	}
}

// resolveHost resolves the hostname part of hostport to a single IP.
// Returns the IP, the original port, and error.
func resolveHost(hostport string) (ip net.IP, port string, err error) {
	host, port, err := net.SplitHostPort(hostport)
	if err != nil {
		return nil, "", err
	}
	addrs, err := net.LookupHost(host)
	if err != nil || len(addrs) == 0 {
		return nil, port, fmt.Errorf("dns lookup %s: %v", host, err)
	}
	ip = net.ParseIP(addrs[0])
	if ip == nil {
		return nil, port, fmt.Errorf("bad IP from lookup: %s", addrs[0])
	}
	return ip, port, nil
}

// ─── Counting writer ──────────────────────────────────────────────────────────

// countingWriter counts bytes written through it.
type countingWriter struct {
	w     io.Writer
	total int64
	mu    sync.Mutex
}

func (c *countingWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.mu.Lock()
	c.total += int64(n)
	c.mu.Unlock()
	return n, err
}

func (c *countingWriter) Total() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.total
}

// ─── handleConnect ────────────────────────────────────────────────────────────

// handleConnect handles HTTPS CONNECT tunnels with TOFU, byte-threshold, grant
// tickets, anti-rebinding, and port policy enforcement.
func handleConnect(w http.ResponseWriter, r *http.Request) {
	hostport := r.Host
	host, portStr, err := net.SplitHostPort(hostport)
	if err != nil {
		http.Error(w, "bad host:port", http.StatusBadRequest)
		return
	}

	// Resolve host → IP for classification.
	resolvedIP, _, err := resolveHost(hostport)
	if err != nil {
		http.Error(w, "dns: "+err.Error(), http.StatusBadGateway)
		return
	}
	normIP := normalizeIP(resolvedIP)
	if normIP == nil {
		http.Error(w, "blocked: rejected IP", http.StatusForbidden)
		return
	}

	if isMetadataIP(normIP) {
		http.Error(w, "blocked: cloud metadata endpoint", http.StatusForbidden)
		return
	}

	internal := isInternal(normIP)

	// Port policy: external targets only on 80/443.
	if !internal {
		if portStr != "80" && portStr != "443" {
			http.Error(w, fmt.Sprintf("blocked: external port %s not allowed", portStr), http.StatusForbidden)
			return
		}

		// TOFU check.
		if !isConfirmed(host) {
			if !callConfirm(host, 0, "tofu_unknown_host") {
				http.Error(w, "blocked by policy", http.StatusForbidden)
				return
			}
			markConfirmed(host)
		}
	}

	// Dial with anti-rebinding control hook.
	dialer := secureDialer(internal)
	dst, err := dialer.Dial("tcp", hostport)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}

	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijack unsupported", http.StatusInternalServerError)
		dst.Close()
		return
	}
	cli, _, err := hj.Hijack()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		dst.Close()
		return
	}
	_, _ = cli.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))

	// For internal targets: plain bidirectional copy.
	if internal {
		go func() { io.Copy(dst, cli); dst.Close() }()
		io.Copy(cli, dst)
		cli.Close()
		return
	}

	// For external targets: count upload bytes and enforce threshold.
	// uploadAuthorized is a per-connection latch: once this connection has been
	// authorized (via grant or confirm), subsequent chunks are forwarded without
	// calling confirm again.
	uploadAuthorized := false
	var uploadTotal int64
	uploadDone := make(chan struct{})

	go func() {
		defer close(uploadDone)
		buf := make([]byte, 32*1024)
		for {
			n, rerr := cli.Read(buf)
			if n > 0 {
				chunk := int64(n)

				// Pre-write gate: if this chunk would push us over the threshold
				// and the connection is not yet authorized, we must authorize
				// BEFORE writing any data to dst.
				if !uploadAuthorized && uploadTotal+chunk > uploadThreshold {
					if hasGrant(host) {
						// Grant covers it; deduct and latch.
						if !consumeGrant(host, chunk, cli) {
							// Budget exhausted; consumeGrant already RST the conn.
							break
						}
						uploadAuthorized = true
					} else {
						// No grant — ask confirm BEFORE writing the chunk.
						if !callConfirm(host, uploadTotal+chunk, "upload_over_threshold") {
							// Deny: RST; the over-limit chunk is never written.
							if tc, ok := cli.(*net.TCPConn); ok {
								tc.SetLinger(0)
							}
							cli.Close()
							dst.Close()
							break
						}
						// Allowed: latch for this connection and register a
						// BOUNDED synthetic grant (observed size + headroom,
						// grantTTL) so a stolen confirm can't become unlimited
						// long-lived egress.
						uploadAuthorized = true
						registerSyntheticGrant(host, uploadTotal+chunk)
					}
				}

				// Write chunk — only reached if authorized or still under threshold.
				_, werr := dst.Write(buf[:n])
				if werr != nil {
					break
				}
				uploadTotal += chunk
			}
			if rerr != nil {
				break
			}
		}
		dst.Close()
	}()

	io.Copy(cli, dst)
	<-uploadDone
	cli.Close()
}

// ─── proxyPlainHTTP ───────────────────────────────────────────────────────────

// proxyPlainHTTP handles plain HTTP proxying with classification, port policy,
// TOFU, anti-rebinding, and hop-by-hop header stripping.
func proxyPlainHTTP(w http.ResponseWriter, r *http.Request) {
	hostport := r.Host
	if !strings.Contains(hostport, ":") {
		hostport = hostport + ":80"
	}
	host, portStr, err := net.SplitHostPort(hostport)
	if err != nil {
		http.Error(w, "bad host", http.StatusBadRequest)
		return
	}

	resolvedIP, _, err := resolveHost(hostport)
	if err != nil {
		http.Error(w, "dns: "+err.Error(), http.StatusBadGateway)
		return
	}
	normIP := normalizeIP(resolvedIP)
	if normIP == nil {
		http.Error(w, "blocked: rejected IP", http.StatusForbidden)
		return
	}

	if isMetadataIP(normIP) {
		http.Error(w, "blocked: cloud metadata endpoint", http.StatusForbidden)
		return
	}

	internal := isInternal(normIP)

	if !internal {
		if portStr != "80" && portStr != "443" {
			http.Error(w, fmt.Sprintf("blocked: external port %s not allowed", portStr), http.StatusForbidden)
			return
		}
		if !isConfirmed(host) {
			if !callConfirm(host, 0, "tofu_unknown_host") {
				http.Error(w, "blocked by policy", http.StatusForbidden)
				return
			}
			markConfirmed(host)
		}
	}

	// Strip hop-by-hop headers from request.
	removeHopByHop(r.Header)
	r.RequestURI = ""

	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return secureDialer(internal).DialContext(ctx, network, addr)
		},
	}

	resp, err := transport.RoundTrip(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Strip hop-by-hop headers from response.
	removeHopByHop(resp.Header)

	for k, vv := range resp.Header {
		for _, v := range vv {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	if _, err := io.Copy(w, resp.Body); err != nil {
		log.Printf("proxyPlainHTTP: copy body: %v", err)
	}
}

// ─── Grant control server ─────────────────────────────────────────────────────

// startGrantServer starts the grant control HTTP server.
// Returns the server and the actual listen address (useful when addr has port 0).
func startGrantServer(addr string) (*http.Server, string, error) {
	mux := http.NewServeMux()
	mux.HandleFunc("/grant", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req grantReq
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad json", http.StatusBadRequest)
			return
		}
		expiry := time.Now().Add(time.Duration(req.TTLSec) * time.Second)
		grantStore.Lock()
		grantStore.m[req.Host] = &ticket{
			MaxBytes: req.MaxBytes,
			Expiry:   expiry,
			Nonce:    req.Nonce,
		}
		grantStore.Unlock()
		w.WriteHeader(http.StatusNoContent)
	})
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, "", err
	}
	actualAddr := ln.Addr().String()
	srv := &http.Server{Addr: actualAddr, Handler: mux}
	go srv.Serve(ln)
	return srv, actualAddr, nil
}

// ─── DNS forwarder (unchanged from Task 1/2) ──────────────────────────────────

// firstNonLinkLocalNameserver reads /etc/resolv.conf and returns the first
// nameserver that is not in the 169.254.0.0/16 range, with port 53 appended.
// Returns "" if none found.
func firstNonLinkLocalNameserver() string {
	f, err := os.Open("/etc/resolv.conf")
	if err != nil {
		return ""
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	linkLocal := cidr("169.254.0.0/16")
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "nameserver") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		ip := net.ParseIP(fields[1])
		if ip != nil && !linkLocal.Contains(ip) {
			return net.JoinHostPort(fields[1], "53")
		}
	}
	return ""
}

// startDNSForwarder starts a minimal UDP+TCP DNS forwarder listening on listenAddr
// and forwarding all queries verbatim to upstream. It returns the actual listen
// address (useful when listenAddr has port 0), a stop function, and any error.
func startDNSForwarder(listenAddr, upstream string) (actualAddr string, stop func(), err error) {
	// UDP listener
	udpConn, err := net.ListenPacket("udp", listenAddr)
	if err != nil {
		return "", nil, err
	}
	// TCP listener on same address/port
	tcpLn, err := net.Listen("tcp", udpConn.LocalAddr().String())
	if err != nil {
		udpConn.Close()
		return "", nil, err
	}

	actualAddr = udpConn.LocalAddr().String()

	stopCh := make(chan struct{})

	// UDP handler: for each incoming packet, dial upstream, forward, read response.
	go func() {
		buf := make([]byte, 4096)
		for {
			select {
			case <-stopCh:
				return
			default:
			}
			n, src, err := udpConn.ReadFrom(buf)
			if err != nil {
				return
			}
			query := make([]byte, n)
			copy(query, buf[:n])
			go func(pkt []byte, clientAddr net.Addr) {
				upConn, err := net.Dial("udp", upstream)
				if err != nil {
					log.Printf("dns udp dial upstream: %v", err)
					return
				}
				defer upConn.Close()
				if _, err := upConn.Write(pkt); err != nil {
					log.Printf("dns udp write upstream: %v", err)
					return
				}
				if err := upConn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
					log.Printf("dns udp set deadline: %v", err)
					return
				}
				resp := make([]byte, 4096)
				rn, err := upConn.Read(resp)
				if err != nil {
					log.Printf("dns udp read upstream: %v", err)
					return
				}
				_, _ = udpConn.WriteTo(resp[:rn], clientAddr)
			}(query, src)
		}
	}()

	// TCP handler: each connection is length-prefixed DNS over TCP.
	go func() {
		for {
			select {
			case <-stopCh:
				return
			default:
			}
			conn, err := tcpLn.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				upConn, err := net.Dial("tcp", upstream)
				if err != nil {
					log.Printf("dns tcp dial upstream: %v", err)
					return
				}
				defer upConn.Close()
				// Read 2-byte length prefix + message from client.
				if err := c.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
					log.Printf("dns tcp set client deadline: %v", err)
					return
				}
				var msgLen uint16
				if err := binary.Read(c, binary.BigEndian, &msgLen); err != nil {
					return
				}
				msg := make([]byte, msgLen)
				if _, err := io.ReadFull(c, msg); err != nil {
					return
				}
				// Forward to upstream with length prefix.
				prefix := make([]byte, 2)
				binary.BigEndian.PutUint16(prefix, msgLen)
				if _, err := upConn.Write(append(prefix, msg...)); err != nil {
					log.Printf("dns tcp write upstream: %v", err)
					return
				}
				// Read upstream response and relay back to client.
				if err := upConn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
					log.Printf("dns tcp set upstream deadline: %v", err)
					return
				}
				var respLen uint16
				if err := binary.Read(upConn, binary.BigEndian, &respLen); err != nil {
					log.Printf("dns tcp read upstream len: %v", err)
					return
				}
				respMsg := make([]byte, respLen)
				if _, err := io.ReadFull(upConn, respMsg); err != nil {
					log.Printf("dns tcp read upstream msg: %v", err)
					return
				}
				binary.BigEndian.PutUint16(prefix, respLen)
				_, _ = c.Write(append(prefix, respMsg...))
			}(conn)
		}
	}()

	stop = func() {
		close(stopCh)
		udpConn.Close()
		tcpLn.Close()
	}
	return actualAddr, stop, nil
}

// ─── main ─────────────────────────────────────────────────────────────────────

func main() {
	listen := flag.String("listen", "169.254.7.1:8888", "HTTP proxy listen address")
	dnsListen := flag.String("dns", "169.254.7.1:53", "DNS forwarder listen address")
	upstream := flag.String("upstream", "", "DNS upstream (default: first non-169.254 nameserver from /etc/resolv.conf with :53)")
	confirmURLFlag := flag.String("confirm-url", "http://127.0.0.1:8282/internal/egress-confirm", "URL for TOFU/threshold confirmation callbacks")
	grantListen := flag.String("grant-listen", "127.0.0.1:8889", "Grant control server listen address")
	tofuTTLFlag := flag.Duration("tofu-ttl", time.Hour, "TTL for auto-TOFU host confirmations")
	grantTTLFlag := flag.Duration("grant-ttl", 10*time.Minute, "TTL for synthetic post-confirm upload grants")
	uploadThreshFlag := flag.Int64("upload-threshold", 65536, "bytes before an external upload asks confirm")
	flag.Parse()

	confirmURL = *confirmURLFlag
	tofuTTL = *tofuTTLFlag
	grantTTL = *grantTTLFlag
	uploadThreshold = *uploadThreshFlag

	// Resolve upstream if not set.
	upstreamAddr := *upstream
	if upstreamAddr == "" {
		upstreamAddr = firstNonLinkLocalNameserver()
	}
	if upstreamAddr == "" {
		upstreamAddr = "8.8.8.8:53"
		log.Printf("dns: no upstream found, falling back to %s", upstreamAddr)
	}

	// Start DNS forwarder.
	_, dnsStop, err := startDNSForwarder(*dnsListen, upstreamAddr)
	if err != nil {
		log.Fatalf("dns forwarder: %v", err)
	}
	defer dnsStop()
	log.Printf("dns-forwarder on %s → %s", *dnsListen, upstreamAddr)

	// Start grant control server.
	_, actualGrantAddr, err := startGrantServer(*grantListen)
	if err != nil {
		log.Fatalf("grant server: %v", err)
	}
	log.Printf("grant-server on %s", actualGrantAddr)

	srv := &http.Server{
		Addr: *listen,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodConnect {
				handleConnect(w, r)
				return
			}
			proxyPlainHTTP(w, r)
		}),
	}
	log.Printf("egress-proxy on %s", *listen)
	log.Fatal(srv.ListenAndServe())
}
