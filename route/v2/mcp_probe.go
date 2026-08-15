package v2

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
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
//
// BuildHandle stays pure and MAY still return "": a stdio server can have no
// args, no url, and a userName that is itself purely non-ASCII/punctuation
// (slugify collapses it to ""). Command is the last automatic signal in that
// case; if even that is empty or noise-only, the caller (probeAndPersist) is
// responsible for substituting a synthetic, per-server handle before
// persisting — never store an empty handle, since the model needs some token
// to name the server by and two empty handles would collide.
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
	if h := slugify(userName); h != "" {
		return h
	}
	// The user-typed name itself collapsed to "" and there was no args/url
	// signal either. The command binary name (e.g. "python3", "uvx") is the
	// last automatic signal available before falling through to the empty
	// string, which the caller must replace with a synthetic handle.
	return stripNoise(path.Base(command))
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
		// The probe never reached Python. This IS the failure outcome (not a
		// persistence side-effect of one), so any error saving it just gets
		// logged: turning it into a 500 here would still discard the 502
		// body below that the browser needs, and leaving it unlogged would
		// hide that probe_state may be stuck at 'probing' (see the "wedge"
		// comment on the SaveSuccess branch below for why that matters).
		if serr := h.svc.MCPRuntime().SaveFailure(m.ID, "agent_unreachable", "agent unreachable"); serr != nil {
			log.Printf("mcp probe %d: failed to persist agent_unreachable result: %v", m.ID, serr)
		}
		body, _ := json.Marshal(map[string]any{"ok": false, "error": "agent unreachable"})
		return probeResult{StatusCode: http.StatusBadGateway, Body: body}, nil
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	var res probeResponse
	if jsonErr := json.Unmarshal(body, &res); jsonErr != nil {
		if serr := h.svc.MCPRuntime().SaveFailure(m.ID, "bad_probe_response", jsonErr.Error()); serr != nil {
			log.Printf("mcp probe %d: failed to persist bad_probe_response result: %v", m.ID, serr)
		}
		return probeResult{StatusCode: http.StatusOK, Body: body}, nil
	}

	if !res.OK {
		errKey := res.ErrorKey
		if errKey == "" {
			errKey = "unknown"
		}
		if serr := h.svc.MCPRuntime().SaveFailure(m.ID, errKey, res.Error); serr != nil {
			log.Printf("mcp probe %d: failed to persist probe failure %q: %v", m.ID, errKey, serr)
		}
		return probeResult{StatusCode: http.StatusOK, Body: body}, nil
	}

	handle := BuildHandle(res.ServerInfo, m.Transport, m.URL, m.Command, args, m.Name)
	if handle == "" {
		// Every automatic signal AND the user-typed name were unusable (see
		// BuildHandle's doc comment). BuildHandle stays pure, so the
		// synthetic fallback -- guaranteed unique and non-empty -- lives
		// here, at the one call site that knows the server's id.
		handle = fmt.Sprintf("server_%d", m.ID)
	}
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
		// The probe itself succeeded, so the plan requires the browser to
		// still see that outcome synchronously -- this must NOT become a
		// 500 that throws away a 125-second probe result over an unrelated
		// DB error. Persisting a best-effort SaveFailure clears the
		// single-flight lock that MarkProbing claimed above and that
		// SaveSuccess's own failure would otherwise leave wedged at
		// 'probing' until process restart (service/mcp_runtime.go:144-147
		// documents this exact class of stuck-lock bug). If that best-effort
		// call ALSO fails, log it but keep the original SaveSuccess error as
		// the one that actually explains what went wrong.
		if ferr := h.svc.MCPRuntime().SaveFailure(m.ID, "persist_failed", serr.Error()); ferr != nil {
			log.Printf("mcp probe %d: SaveSuccess failed (%v) AND the best-effort SaveFailure to release the lock also failed: %v", m.ID, serr, ferr)
		} else {
			log.Printf("mcp probe %d: probe succeeded but persisting the result failed: %v", m.ID, serr)
		}
	}
	return probeResult{StatusCode: http.StatusOK, Body: body}, nil
}

// --- migration: backfill identity cards for pre-existing servers ---

// StartMigrationBackfill launches, in its own background goroutine, the
// one-time sweep that gives every pre-existing MCP server (one that predates
// this progressive-disclosure feature and therefore has no mcp_server_runtime
// row at all) its first identity card. The caller (route/v2.go, at process
// startup, mirroring how AgentHandler.StartHealthMonitor is wired) must never
// wait on this: a single stdio server's probe can take up to ~120s (see
// probeAndPersist's timeout comment above), and there may be many such
// servers, so running the sweep on the startup path could hang service
// start for many minutes.
func (h *MCPHandler) StartMigrationBackfill() {
	go func() {
		if err := h.migrateBackfillIdentityCards(); err != nil {
			log.Printf("mcp migration sweep: %v", err)
		}
	}()
}

// migrateBackfillIdentityCards is the sweep itself, run synchronously on the
// goroutine StartMigrationBackfill spawns (tests call it directly so they can
// observe its effects deterministically instead of racing a detached
// goroutine). Servers get here after an upgrade: they existed before Task 7's
// "probe on add" mechanism shipped, so the model's L1 catalogue would show
// them as "not yet probed" forever without this. The fix is simply to run the
// exact same probeAndPersist flow a newly-added server gets — no new
// mechanism.
//
// Only servers with NO runtime row at all are candidates (the LEFT JOIN
// below). This is deliberately narrower than "needs a fresh probe": Task 8's
// TTL self-check (route/v2/mcp.go's Runtime handler) already re-probes a
// server whose listing has merely expired, and also already treats a
// missing runtime row as trivially expired, so it independently backfills
// never-probed servers the first time anyone calls Runtime for them. This
// sweep is therefore belt-and-braces, not the only backfill path — and the
// two can safely run concurrently: probeAndPersist's first step,
// MarkProbing, is an atomic UPSERT that claims the single-flight lock by
// INSERTing the row with probe_state='probing', so exactly one of the two
// wins the claim for any given server and the loser costs one Get query and
// exits nil. No second locking mechanism is layered on top of it here.
//
// Serial by design: a stdio server's probe shells out to an npx/uvx child
// process and can take up to ~120s. Firing off every candidate concurrently
// would spawn that many child processes at once and could saturate a NAS's
// disk and network; probing them one at a time, inside this single
// goroutine, keeps the blast radius to one child process no matter how many
// servers need backfilling.
func (h *MCPHandler) migrateBackfillIdentityCards() error {
	rows, err := h.svc.DB().Query(`
		SELECT s.id, s.user_id, s.name, s.transport, s.url, s.command, s.args,
		       s.env, s.headers, s.enabled, s.created_at, s.updated_at
		FROM mcp_servers s
		LEFT JOIN mcp_server_runtime r ON r.server_id = s.id
		WHERE r.server_id IS NULL AND s.enabled = 1
		ORDER BY s.id`)
	if err != nil {
		return err
	}
	var pending []*service.McpServer
	for rows.Next() {
		m := &service.McpServer{}
		var enabled int
		if scanErr := rows.Scan(&m.ID, &m.UserID, &m.Name, &m.Transport, &m.URL, &m.Command,
			&m.Args, &m.Env, &m.Headers, &enabled, &m.CreatedAt, &m.UpdatedAt); scanErr != nil {
			rows.Close()
			return scanErr
		}
		m.Enabled = enabled == 1
		pending = append(pending, m)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	rows.Close()

	// One at a time, on purpose — see the doc comment above.
	for _, m := range pending {
		if _, err := h.probeAndPersist(m, h.decryptMap(m.Env), h.decryptMap(m.Headers)); err != nil {
			log.Printf("mcp migration sweep: probe for server %d failed: %v", m.ID, err)
		}
	}
	return nil
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
