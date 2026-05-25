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

type ParserClient struct {
	discoveryPath string
	mu            sync.RWMutex
	cachedURL     string
	http          *http.Client
	httpLong      *http.Client // for slow paths (test analyze, model loads)
}

func NewParserClient(discoveryPath string) *ParserClient {
	return &ParserClient{
		discoveryPath: discoveryPath,
		http:          &http.Client{Timeout: 5 * time.Second},
		httpLong:      &http.Client{Timeout: 120 * time.Second},
	}
}

func (c *ParserClient) baseURL() (string, error) {
	c.mu.RLock()
	cached := c.cachedURL
	c.mu.RUnlock()
	if cached != "" {
		return cached, nil
	}
	return c.reloadDiscovery()
}

func (c *ParserClient) reloadDiscovery() (string, error) {
	b, err := os.ReadFile(c.discoveryPath)
	if err != nil {
		return "", fmt.Errorf("read parser.url: %w", err)
	}
	url := string(bytes.TrimSpace(b))
	c.mu.Lock()
	c.cachedURL = url
	c.mu.Unlock()
	return url, nil
}

// SetCachedBaseURL — 测试用,直接注入 cache(模拟陈旧缓存场景)
func (c *ParserClient) SetCachedBaseURL(url string) {
	c.mu.Lock()
	c.cachedURL = url
	c.mu.Unlock()
}

func (c *ParserClient) do(ctx context.Context, method, path string, body []byte) ([]byte, int, error) {
	base, err := c.baseURL()
	if err != nil {
		return nil, 0, err
	}
	resp, err := c.tryOnce(ctx, method, base+path, body)
	if err == nil {
		return c.readBody(resp)
	}
	// 重读 discovery 后再试一次
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

func (c *ParserClient) tryOnce(ctx context.Context, method, url string, body []byte) (*http.Response, error) {
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

func (c *ParserClient) readBody(resp *http.Response) ([]byte, int, error) {
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}
	return b, resp.StatusCode, nil
}

// Forward proxies an arbitrary method + body with a caller-supplied
// Content-Type (e.g. multipart/form-data for file uploads). Falls back to
// the same discovery-retry policy as `do`.
func (c *ParserClient) Forward(method, path, contentType string, body []byte) ([]byte, int, error) {
	base, err := c.baseURL()
	if err != nil {
		return nil, 0, err
	}
	resp, err := c.tryOnceCT(method, base+path, contentType, body)
	if err == nil {
		return c.readBody(resp)
	}
	if _, rerr := c.reloadDiscovery(); rerr != nil {
		return nil, 0, err
	}
	base, _ = c.baseURL()
	resp, err = c.tryOnceCT(method, base+path, contentType, body)
	if err != nil {
		return nil, 0, err
	}
	return c.readBody(resp)
}

func (c *ParserClient) tryOnceCT(method, url, contentType string, body []byte) (*http.Response, error) {
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
	return c.httpLong.Do(req)
}

func (c *ParserClient) Get(path string) ([]byte, int, error) {
	return c.do(nil, "GET", path, nil)
}

func (c *ParserClient) Post(path string, body []byte) ([]byte, int, error) {
	return c.do(nil, "POST", path, body)
}

func (c *ParserClient) GetWithContext(ctx context.Context, path string) ([]byte, int, error) {
	return c.do(ctx, "GET", path, nil)
}

func (c *ParserClient) PostWithContext(ctx context.Context, path string, body []byte) ([]byte, int, error) {
	return c.do(ctx, "POST", path, body)
}
