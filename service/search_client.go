package service

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"time"
)

// SearchClient mirrors ParserClient: discovers the Search service via a URL
// file written by that service at startup and forwards HTTP requests to it.
type SearchClient struct {
	discoveryPath string
	mu            sync.RWMutex
	cachedURL     string
	http          *http.Client
	httpLong      *http.Client // for slow paths (text search: embed + rerank)
}

func NewSearchClient(discoveryPath string) *SearchClient {
	return &SearchClient{
		discoveryPath: discoveryPath,
		http:          &http.Client{Timeout: 5 * time.Second},
		httpLong:      &http.Client{Timeout: 120 * time.Second},
	}
}

func (c *SearchClient) baseURL() (string, error) {
	c.mu.RLock()
	cached := c.cachedURL
	c.mu.RUnlock()
	if cached != "" {
		return cached, nil
	}
	return c.reloadDiscovery()
}

func (c *SearchClient) reloadDiscovery() (string, error) {
	b, err := os.ReadFile(c.discoveryPath)
	if err != nil {
		return "", fmt.Errorf("read search.url: %w", err)
	}
	url := string(bytes.TrimSpace(b))
	c.mu.Lock()
	c.cachedURL = url
	c.mu.Unlock()
	return url, nil
}

// SetCachedBaseURL is for tests — allows injecting a known base URL directly.
func (c *SearchClient) SetCachedBaseURL(url string) {
	c.mu.Lock()
	c.cachedURL = url
	c.mu.Unlock()
}

func (c *SearchClient) do(ctx context.Context, method, path string, body []byte) ([]byte, int, error) {
	base, err := c.baseURL()
	if err != nil {
		return nil, 0, err
	}
	resp, err := c.tryOnce(ctx, method, base+path, body)
	if err == nil {
		return c.readBody(resp)
	}
	// Re-read discovery and retry once.
	if _, rerr := c.reloadDiscovery(); rerr != nil {
		return nil, 0, err
	}
	base, _ = c.baseURL()
	resp, err = c.tryOnce(ctx, method, base+path, body)
	if err != nil {
		return nil, 0, err
	}
	return c.readBody(resp)
}

func (c *SearchClient) tryOnce(ctx context.Context, method, url string, body []byte) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	var req *http.Request
	var err error
	if ctx != nil {
		req, err = http.NewRequestWithContext(ctx, method, url, reader)
	} else {
		req, err = http.NewRequest(method, url, reader)
	}
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return c.http.Do(req)
}

func (c *SearchClient) readBody(resp *http.Response) ([]byte, int, error) {
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}
	return b, resp.StatusCode, nil
}

func (c *SearchClient) GetWithContext(ctx context.Context, path string) ([]byte, int, error) {
	return c.do(ctx, "GET", path, nil)
}

// Forward proxies an arbitrary method/path to the Search service using the
// long-timeout client, preserving the caller's Content-Type and forwarding the
// given headers (e.g. X-NimoOS-User-ID). Tries once, reloads discovery, retries once.
func (c *SearchClient) Forward(method, path, contentType string, body []byte, headers map[string]string) ([]byte, int, error) {
	base, err := c.baseURL()
	if err != nil {
		return nil, 0, err
	}
	resp, err := c.tryOnceCT(method, base+path, contentType, body, headers)
	if err == nil {
		return c.readBody(resp)
	}
	if _, rerr := c.reloadDiscovery(); rerr != nil {
		return nil, 0, err
	}
	base, _ = c.baseURL()
	resp, err = c.tryOnceCT(method, base+path, contentType, body, headers)
	if err != nil {
		return nil, 0, err
	}
	return c.readBody(resp)
}

func (c *SearchClient) tryOnceCT(method, url, contentType string, body []byte, headers map[string]string) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, url, reader)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	return c.httpLong.Do(req)
}
