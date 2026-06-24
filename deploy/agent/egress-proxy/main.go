package main

import (
	"flag"
	"io"
	"log"
	"net"
	"net/http"
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

func main() {
	listen := flag.String("listen", "169.254.7.1:8888", "")
	flag.Parse()
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
