package main

import (
	"bufio"
	"encoding/binary"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

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

func cidr(s string) *net.IPNet {
	_, n, err := net.ParseCIDR(s)
	if err != nil {
		panic(err)
	}
	return n
}

func isInternal(ip net.IP) bool {
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

// handleConnect 处理 HTTPS CONNECT 隧道(P0:仅转发,字节量/ TOFU 见 Task 3)
func handleConnect(w http.ResponseWriter, r *http.Request) {
	dst, err := net.Dial("tcp", r.Host)
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
	go func() { io.Copy(dst, cli); dst.Close() }()
	io.Copy(cli, dst)
	cli.Close()
}

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

func main() {
	listen := flag.String("listen", "169.254.7.1:8888", "HTTP proxy listen address")
	dnsListen := flag.String("dns", "169.254.7.1:53", "DNS forwarder listen address")
	upstream := flag.String("upstream", "", "DNS upstream (default: first non-169.254 nameserver from /etc/resolv.conf with :53)")
	flag.Parse()

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

func proxyPlainHTTP(w http.ResponseWriter, r *http.Request) {
	r.RequestURI = ""
	resp, err := http.DefaultTransport.RoundTrip(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
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
