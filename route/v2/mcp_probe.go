package v2

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"strings"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
)

var (
	slugRe     = regexp.MustCompile(`[^a-z0-9]+`)
	noiseWords = map[string]bool{"mcp": true, "server": true, "mcpserver": true}
)

// slugify keeps the same semantics as the Python side's mcp_client.client._slug:
// lowercase, collapse non-alphanumerics into underscores, trim leading/trailing
// underscores.
func slugify(s string) string {
	return strings.Trim(slugRe.ReplaceAllString(strings.ToLower(strings.TrimSpace(s)), "_"), "_")
}

// stripNoise drops noise segments like "mcp" / "server" so that
// "github-mcp-server" becomes "github".
func stripNoise(s string) string {
	parts := strings.Split(slugify(s), "_")
	kept := parts[:0]
	for _, p := range parts {
		if p != "" && !noiseWords[p] {
			kept = append(kept, p)
		}
	}
	if len(kept) == 0 {
		return slugify(s)
	}
	return strings.Join(kept, "_")
}

// BuildHandle produces the token the model uses to name a server.
//
// Key decision (design doc §4.4): the handle is derived from the server's
// SELF-REPORTED identity, not from the name the user typed. The reader of L0
// and expand_tools is the model, and the model never sees the settings page —
// a user may well have named the server "测试1" (Chinese for "test 1"). The
// user-typed name is only the last-resort fallback, once every automatic
// signal is unavailable, and otherwise only serves as a disambiguating suffix
// in the UI layer.
func BuildHandle(serverInfo map[string]string, transport, rawURL, command string, args []string, userName string) string {
	if serverInfo != nil {
		if n := serverInfo["name"]; n != "" {
			if h := stripNoise(n); h != "" {
				return h
			}
		}
	}
	// npm / uvx package name: written by a human for humans, so it carries very
	// high semantic density, and it costs zero network calls to read.
	for _, a := range args {
		if strings.HasPrefix(a, "-") {
			continue
		}
		if strings.Contains(a, "/") || strings.Contains(a, "-") {
			if h := stripNoise(path.Base(a)); h != "" {
				return h
			}
		}
	}
	if rawURL != "" {
		if u, err := url.Parse(rawURL); err == nil && u.Host != "" {
			host := u.Hostname()
			labels := strings.Split(host, ".")
			// Pick the meaningful label: mcp.notion.com -> notion (drop the "mcp"
			// prefix and the TLD).
			for _, l := range labels {
				if !noiseWords[l] && l != "www" && len(l) > 2 && !isTLD(l) {
					return slugify(l)
				}
			}
			return stripNoise(host)
		}
	}
	return slugify(userName)
}

func isTLD(s string) bool {
	switch s {
	case "com", "org", "net", "io", "ai", "dev", "cn", "co":
		return true
	}
	return false
}

// ProbeSchema is one element of the `schemas` array returned by Python's
// /agent/mcp/test.
type ProbeSchema struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	InputSchema any    `json:"input_schema"`
}

const summaryMaxLen = 200

// BuildSummary pre-renders the one-line blurb used at disclosure level L1. See
// the fallback chain in design doc §4.2. It is computed once, at persist
// time, so every reader downstream pays zero computation for it.
func BuildSummary(instructions string, serverInfo map[string]string,
	transport, rawURL, command string, args []string, schemas []ProbeSchema) string {

	if s := firstSentence(instructions); s != "" {
		return s
	}
	if serverInfo != nil {
		for _, k := range []string{"description", "title"} {
			if v := strings.TrimSpace(serverInfo[k]); v != "" {
				return truncate(v, summaryMaxLen)
			}
		}
	}
	// Connection target: zero network calls, always available.
	if target := connectionTarget(transport, rawURL, command, args); target != "" {
		if len(schemas) > 0 {
			return truncate(target+" — "+firstSentence(schemas[0].Description), summaryMaxLen)
		}
		return truncate(target, summaryMaxLen)
	}
	if len(schemas) > 0 {
		return truncate(firstSentence(schemas[0].Description), summaryMaxLen)
	}
	return ""
}

func connectionTarget(transport, rawURL, command string, args []string) string {
	if rawURL != "" {
		if u, err := url.Parse(rawURL); err == nil && u.Host != "" {
			return u.Hostname()
		}
	}
	for _, a := range args {
		if !strings.HasPrefix(a, "-") {
			return a
		}
	}
	return command
}

// firstSentence walks runes rather than using strings.IndexAny + s[i] --
// the latter returns a byte offset, which cuts a multi-byte terminator like
// "。" in half and produces garbled output.
func firstSentence(s string) string {
	s = strings.TrimSpace(strings.ReplaceAll(s, "\n", " "))
	if s == "" {
		return ""
	}
	for i, r := range s {
		switch r {
		case '.', '。', '!', '！', '?', '？':
			return truncate(strings.TrimSpace(s[:i+len(string(r))]), summaryMaxLen)
		}
	}
	return truncate(s, summaryMaxLen)
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

// --- probe orchestration ---

// probeResponse is the shape of Python's POST /agent/mcp/test response. See
// agent/mcp_client/client.py's test_server/_test_server_inner for the
// authoritative definition.
type probeResponse struct {
	OK                bool               `json:"ok"`
	Error             string             `json:"error"`
	ErrorKey          string             `json:"error_key"`
	ToolCount         int                `json:"tool_count"`
	Tools             []string           `json:"tools"`
	ProtocolEra       string             `json:"protocol_era"`
	ProtocolVersion   string             `json:"protocol_version"`
	SupportedVersions []string           `json:"supported_versions"`
	Instructions      string             `json:"instructions"`
	ServerInfo        map[string]string  `json:"server_info"`
	TTLSec            int64              `json:"ttl_sec"`
	ToolMetas         []service.ToolMeta `json:"tool_metas"`
	Schemas           []ProbeSchema      `json:"schemas"`
}

// probeResult is what probeAndPersist hands back to the caller: the exact
// status code and body the HTTP handler should relay to the browser.
type probeResult struct {
	StatusCode int
	Body       []byte
}

// protocolModeFor decides the value a later mcp_client.Client(mode=...) call
// should receive when reconnecting to this server, so that call can skip the
// discover round-trip the probe already paid for.
//
//   - era == "modern": pin the exact negotiated version string. The SDK
//     accepts a concrete version as mode, so the next connect dials straight
//     into it instead of re-running the server/discover probe.
//   - era == "legacy": the server doesn't understand discover at all, so
//     asking again would just waste a round trip; pin "legacy".
//   - anything else (including "unknown", or a "modern" era with no version
//     reported): fall back to "auto", the SDK's full dual-protocol
//     negotiation — the only safe choice when we don't have a concrete value
//     to pin.
func protocolModeFor(era, version string) string {
	switch era {
	case "modern":
		if version != "" {
			return version
		}
	case "legacy":
		return "legacy"
	}
	return "auto"
}

// probeAndPersist calls the Python agent's /agent/mcp/test for m and persists
// the result as this server's runtime row before returning. env/headers are
// the already-decrypted plaintext maps (callers must not decrypt twice; see
// route/v2/mcp.go's decryptMap).
//
// Order of operations: MarkProbing claims the single-flight lock first; if a
// probe is already running for this server, this returns the last persisted
// observation instead of starting a second concurrent probe. Both save paths
// below (SaveSuccess / SaveFailure) clear the lock themselves, so there is no
// path that claims it and leaves it held.
func (h *MCPHandler) probeAndPersist(m *service.McpServer, env, headers map[string]string) (probeResult, error) {
	claimed, err := h.svc.MCPRuntime().MarkProbing(m.ID)
	if err != nil {
		return probeResult{}, err
	}
	if !claimed {
		return h.probingInProgressResult(m.ID), nil
	}

	var args []string
	_ = json.Unmarshal([]byte(m.Args), &args)
	payload, _ := json.Marshal(map[string]any{
		"id": m.ID, "name": m.Name, "transport": m.Transport, "url": m.URL,
		"command": m.Command, "args": args,
		"env":     env,
		"headers": headers,
	})
	// Must exceed Python's outer backstop (client.py: TEST_TIMEOUT=41 /
	// STDIO_TEST_TIMEOUT=120) so Python cancels first and releases the subprocess and
	// socket, rather than Go abandoning a request that keeps running. Those Python
	// values are themselves connect phase + list phase + close phase + slack -- see
	// the constant block in client.py.
	timeout := 43 * time.Second
	if m.Transport == "stdio" {
		timeout = 125 * time.Second
	}
	client := &http.Client{Timeout: timeout}
	resp, err := client.Post(h.agentURL+"/agent/mcp/test", "application/json", bytes.NewReader(payload))
	if err != nil {
		if serr := h.svc.MCPRuntime().SaveFailure(m.ID, "agent_unreachable", "agent unreachable"); serr != nil {
			return probeResult{}, serr
		}
		body, _ := json.Marshal(map[string]any{"ok": false, "error": "agent unreachable"})
		return probeResult{StatusCode: http.StatusBadGateway, Body: body}, nil
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	var res probeResponse
	if jsonErr := json.Unmarshal(body, &res); jsonErr != nil {
		if serr := h.svc.MCPRuntime().SaveFailure(m.ID, "bad_probe_response", jsonErr.Error()); serr != nil {
			return probeResult{}, serr
		}
		return probeResult{StatusCode: http.StatusOK, Body: body}, nil
	}

	if !res.OK {
		errKey := res.ErrorKey
		if errKey == "" {
			errKey = "unknown"
		}
		if serr := h.svc.MCPRuntime().SaveFailure(m.ID, errKey, res.Error); serr != nil {
			return probeResult{}, serr
		}
		return probeResult{StatusCode: http.StatusOK, Body: body}, nil
	}

	handle := BuildHandle(res.ServerInfo, m.Transport, m.URL, m.Command, args, m.Name)
	summary := BuildSummary(res.Instructions, res.ServerInfo, m.Transport, m.URL, m.Command, args, res.Schemas)
	schemasJSON, _ := json.Marshal(res.Schemas)

	runtime := &service.McpServerRuntime{
		ServerID:      m.ID,
		ServerName:    res.ServerInfo["name"],
		ServerTitle:   res.ServerInfo["title"],
		ServerVersion: res.ServerInfo["version"],
		Handle:        handle,
		Instructions:  res.Instructions,
		Summary:       summary,
		TTLSec:        res.TTLSec,
		ConfigFP:      service.ConfigFP(m.Transport, m.URL, m.Command, args, env, headers),
		IdentityFP:    service.IdentityFP(m.Transport, m.URL, m.Command, args, env, headers),
		ProtocolMode:  protocolModeFor(res.ProtocolEra, res.ProtocolVersion),
		ProtocolEra:   res.ProtocolEra,
	}
	if serr := h.svc.MCPRuntime().SaveSuccess(runtime, res.ToolMetas, string(schemasJSON)); serr != nil {
		return probeResult{}, serr
	}
	return probeResult{StatusCode: http.StatusOK, Body: body}, nil
}

// probingInProgressResult builds the response returned when MarkProbing finds
// a probe already in flight for this server: the caller must not block
// forever waiting on someone else's probe, so it gets back the last
// persisted observation (if any) instead.
func (h *MCPHandler) probingInProgressResult(serverID int64) probeResult {
	resp := map[string]any{
		"ok":        false,
		"probing":   true,
		"error":     "a probe for this server is already running",
		"error_key": "probe_in_progress",
	}
	if r, _ := h.svc.MCPRuntime().Get(serverID); r != nil {
		var tools []service.ToolMeta
		_ = json.Unmarshal([]byte(r.ToolsJSON), &tools)
		names := make([]string, len(tools))
		for i, tl := range tools {
			names[i] = tl.Name
		}
		resp["ok"] = r.ProbeState == "ok"
		resp["tool_count"] = len(tools)
		resp["tools"] = names
		resp["handle"] = r.Handle
		resp["summary"] = r.Summary
		resp["protocol_era"] = r.ProtocolEra
	}
	body, _ := json.Marshal(resp)
	return probeResult{StatusCode: http.StatusOK, Body: body}
}
