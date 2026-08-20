package v2

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/NimoTech/NimoOS-AI/service"
	"github.com/labstack/echo/v4"
)

// The "test connection" button must report what the server answers RIGHT NOW.
// Everything in this file pins that contract against the two ways it can be
// faked: reporting the last persisted observation as if it were this
// request's own result, and reporting a stale row after waiting for someone
// else's probe.
//
// Why this is easy to hit in production rather than a corner case: the
// agent's TTL self-check (route/v2/mcp.go's Runtime handler) re-probes any
// server whose listing has expired, and SCHEMA_TTL_MIN clamps that TTL to 60
// seconds — so for a server with a short TTL there is very often a probe
// already in flight when the user clicks the button.

// testEchoWithCtx is testEcho (mcp_probe_test.go) with a caller-supplied
// request context, so a test can model the browser hanging up mid-wait.
func testEchoWithCtx(ctx context.Context, method, id, uid string) (echo.Context, *httptest.ResponseRecorder) {
	e := echo.New()
	req := httptest.NewRequest(method, "/", nil).WithContext(ctx)
	if uid != "" {
		req.Header.Set("X-NimoOS-User-ID", uid)
	}
	rec := httptest.NewRecorder()
	c := e.NewContext(req, rec)
	c.SetParamNames("id")
	c.SetParamValues(id)
	return c, rec
}

// seedCachedSuccess gives the server a persisted, successful observation —
// the row a contended /test request must NOT dress up as its own result.
func seedCachedSuccess(t *testing.T, svc service.Services, serverID int64) {
	t.Helper()
	if err := svc.MCPRuntime().SaveSuccess(&service.McpServerRuntime{
		ServerID: serverID, Handle: "github", Summary: "cached summary",
		ProtocolEra: "modern", ProtocolMode: "2025-06-18", TTLSec: 60,
	}, []service.ToolMeta{{Name: "cached_tool", SchemaHash: "sh", DescHash: "dh"}}, "[]"); err != nil {
		t.Fatalf("seed cached runtime row: %v", err)
	}
}

// mustHoldProbeLock claims the single-flight lock the way an in-flight probe
// does, so the request under test loses the claim.
func mustHoldProbeLock(t *testing.T, svc service.Services, serverID int64) {
	t.Helper()
	claimed, err := svc.MCPRuntime().MarkProbing(serverID)
	if err != nil || !claimed {
		t.Fatalf("failed to simulate an in-flight probe: claimed=%v err=%v", claimed, err)
	}
}

func createProbeServer(t *testing.T, svc service.Services) *service.McpServer {
	t.Helper()
	m := &service.McpServer{
		UserID: "u1", Name: "gh", Transport: "http", URL: "https://x/mcp",
		Args: "[]", Env: "{}", Enabled: true,
	}
	if err := svc.MCP().CreateMcpServer(m); err != nil {
		t.Fatalf("create: %v", err)
	}
	return m
}

// TestTest_LockContentionMustNotReturnCachedToolData is the core defect: on
// lock contention the handler used to read the persisted runtime row and
// report probe_state=="ok" as this request's `ok`, plus that row's cached
// tool list — so the settings page rendered a success panel built entirely
// from cache while the actual probe was still running. The contended branch
// must say only "a probe is in flight", with no observation attached.
func TestTest_LockContentionMustNotReturnCachedToolData(t *testing.T) {
	svc := mcpTestSvc(t)
	m := createProbeServer(t, svc)
	seedCachedSuccess(t, svc, m.ID)
	mustHoldProbeLock(t, svc, m.ID)

	// A contended request must never dial: the whole point of the
	// single-flight lock is that the probe already running owns the dial.
	var dials atomic.Int64
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dials.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"tool_count":1,"tools":["fresh_tool"]}`))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)

	// The browser hung up (the user switched servers) — the waiter releases
	// at once, which is the fast, deterministic way into the same branch a
	// 10s wait-budget expiry reaches.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	c, rec := testEchoWithCtx(ctx, http.MethodPost, fmt.Sprintf("%d", m.ID), "u1")
	if err := h.Test(c); err != nil {
		t.Fatalf("Test: %v", err)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("Test code = %d, body = %s", rec.Code, rec.Body.String())
	}

	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal response: %v body=%s", err, rec.Body.String())
	}
	if body["ok"] != false {
		t.Fatalf("ok = %v, want false: a probe was in flight, so this request has no result of "+
			"its own — reporting the cached row's probe_state as `ok` renders a fake success "+
			"panel. body=%s", body["ok"], rec.Body.String())
	}
	if body["probing"] != true {
		t.Fatalf("probing = %v, want true — the UI can only tell this apart from a real "+
			"failure by this flag. body=%s", body["probing"], rec.Body.String())
	}
	if body["error_key"] != "probe_in_progress" {
		t.Fatalf("error_key = %v, want probe_in_progress. body=%s", body["error_key"], rec.Body.String())
	}
	for _, leaked := range []string{"tools", "tool_count", "handle", "summary", "protocol_era"} {
		if _, present := body[leaked]; present {
			t.Fatalf("body carries cached field %q — the contended branch must attach NO "+
				"observation, or the UI cannot tell cache from a fresh probe. body=%s",
				leaked, rec.Body.String())
		}
	}
	if n := dials.Load(); n != 0 {
		t.Fatalf("the contended request dialled the agent %d time(s) — the in-flight probe "+
			"owns the dial", n)
	}

	// A waiter giving up must never disturb the probe it was waiting for.
	rt, err := svc.MCPRuntime().Get(m.ID)
	if err != nil {
		t.Fatalf("get runtime: %v", err)
	}
	if rt == nil || rt.ProbeState != "probing" {
		t.Fatalf("probe_state = %+v, want still 'probing' — a waiter that walks away must not "+
			"release or cancel the probe holding the lock", rt)
	}
}

// freshProbeBody is what the fake agent answers with once the test releases
// it: a listing that shares nothing with the cached row seeded above, so an
// assertion on any field tells cache and fresh probe apart.
const freshProbeBody = `{
	"ok": true, "tool_count": 1, "tools": ["fresh_tool"],
	"protocol_era": "modern", "protocol_version": "2025-06-18",
	"supported_versions": ["2025-06-18"],
	"instructions": "Fresh listing. Everything after the first sentence is dropped.",
	"server_info": {"name": "notion-mcp-server", "title": "Notion", "version": "9.9.9"},
	"ttl_sec": 300,
	"tool_metas": [{"name": "fresh_tool", "schema_hash": "sh2", "desc_hash": "dh2"}],
	"schemas": [{"name": "fresh_tool", "description": "Fresh.", "input_schema": {"type": "object"}}]
}`

// testOutcome carries one h.Test call's result off its goroutine — t.Fatalf
// may only be called from the test's own goroutine.
type testOutcome struct {
	rec *httptest.ResponseRecorder
	err error
}

func runTestHandler(h *MCPHandler, serverID int64) <-chan testOutcome {
	out := make(chan testOutcome, 1)
	go func() {
		c, rec := testEcho(http.MethodPost, fmt.Sprintf("%d", serverID), "u1")
		out <- testOutcome{rec: rec, err: h.Test(c)}
	}()
	return out
}

func recvOutcome(t *testing.T, ch <-chan testOutcome, what string) *httptest.ResponseRecorder {
	t.Helper()
	select {
	case o := <-ch:
		if o.err != nil {
			t.Fatalf("%s: Test returned %v", what, o.err)
		}
		return o.rec
	case <-time.After(10 * time.Second):
		t.Fatalf("%s never returned", what)
		return nil
	}
}

// waitForProbeState blocks until the runtime row reports want, so a test can
// synchronize on "the probe has claimed the lock" instead of sleeping.
func waitForProbeState(t *testing.T, svc service.Services, serverID int64, want string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		r, err := svc.MCPRuntime().Get(serverID)
		if err != nil {
			t.Fatalf("get runtime: %v", err)
		}
		if r != nil && r.ProbeState == want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("probe_state never became %q", want)
}

// decodeBody unmarshals a recorded response body, failing the test on garbage.
func decodeBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal response: %v body=%s", err, rec.Body.String())
	}
	return body
}

// TestTest_WokenWaiterReturnsTheFreshlyPersistedRow is the other half of the
// contract: a request that lost the single-flight claim and then waited must
// answer with the observation the probe it waited for just committed — not
// with the row that was there before, and not by dialling a second time.
//
// Both requests run through the real handler, so the wakeup under test is the
// production broadcast inside dialAndPersist, not a hand-rolled one.
func TestTest_WokenWaiterReturnsTheFreshlyPersistedRow(t *testing.T) {
	svc := mcpTestSvc(t)
	m := createProbeServer(t, svc)
	seedCachedSuccess(t, svc, m.ID)

	release := make(chan struct{})
	var dials atomic.Int64
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dials.Add(1)
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(freshProbeBody))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)

	first := runTestHandler(h, m.ID)
	// The first request holds the lock and is now parked in the fake agent.
	waitForProbeState(t, svc, m.ID, "probing")

	second := runTestHandler(h, m.ID)
	// The second request cannot honestly answer yet: the probe that owns the
	// dial has published nothing. Returning here at all means it read the
	// persisted row.
	select {
	case o := <-second:
		t.Fatalf("the contended request answered before the in-flight probe published anything: "+
			"%s (err=%v) — that answer can only have come from cache", o.rec.Body.String(), o.err)
	case <-time.After(150 * time.Millisecond):
	}

	close(release) // the probe completes, persists, and broadcasts
	firstRec := recvOutcome(t, first, "the probing request")
	secondRec := recvOutcome(t, second, "the waiting request")

	if firstRec.Code != http.StatusOK {
		t.Fatalf("probing request code = %d body = %s", firstRec.Code, firstRec.Body.String())
	}
	body := decodeBody(t, secondRec)
	if body["ok"] != true {
		t.Fatalf("ok = %v, want true — the probe it waited for succeeded. body=%s",
			body["ok"], secondRec.Body.String())
	}
	if body["probing"] == true {
		t.Fatalf("probing = true although a fresh result arrived. body=%s", secondRec.Body.String())
	}
	if got := body["tool_count"]; got != float64(1) {
		t.Fatalf("tool_count = %v, want 1. body=%s", got, secondRec.Body.String())
	}
	tools, _ := body["tools"].([]any)
	if len(tools) != 1 || tools[0] != "fresh_tool" {
		t.Fatalf("tools = %v, want [fresh_tool] — [cached_tool] means it answered from the "+
			"pre-probe row instead of the one just committed. body=%s", body["tools"], secondRec.Body.String())
	}
	if body["handle"] != "notion" {
		t.Fatalf("handle = %v, want notion (the freshly probed identity; the cached row said "+
			"github). body=%s", body["handle"], secondRec.Body.String())
	}
	if body["summary"] != "Fresh listing." {
		t.Fatalf("summary = %v, want the fresh listing's first sentence. body=%s",
			body["summary"], secondRec.Body.String())
	}
	if body["protocol_era"] != "modern" || body["protocol_version"] != "2025-06-18" {
		t.Fatalf("protocol_era/protocol_version = %v/%v, want modern/2025-06-18 so the settings "+
			"page can still render its protocol line. body=%s",
			body["protocol_era"], body["protocol_version"], secondRec.Body.String())
	}
	if body["config_changed"] != false {
		t.Fatalf("config_changed = %v, want false — nobody edited this server while the probe "+
			"ran, so its result describes the config the user is looking at. body=%s",
			body["config_changed"], secondRec.Body.String())
	}
	if n := dials.Load(); n != 1 {
		t.Fatalf("the agent was dialled %d times, want exactly 1 — the waiter must consume the "+
			"in-flight probe's result, not start a second probe", n)
	}
}

// TestTest_WokenWaiterFlagsAConfigEditedMidProbe covers the one case where a
// freshly committed row still is not an answer about the config on screen:
// the user edited the server (a rotated token, a corrected URL) while the
// probe was in flight, so what committed describes the PREVIOUS config.
//
// The handler reports it as a flag and stops. It deliberately does NOT retry
// until the fingerprints agree: a user fixing a typo one keystroke at a time
// would keep invalidating each new probe, and the request would spin instead
// of ever answering. One wait, one answer, and the UI asks for another test.
func TestTest_WokenWaiterFlagsAConfigEditedMidProbe(t *testing.T) {
	svc := mcpTestSvc(t)
	m := createProbeServer(t, svc)

	release := make(chan struct{})
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(freshProbeBody))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)

	first := runTestHandler(h, m.ID)
	waitForProbeState(t, svc, m.ID, "probing")

	// The user edits the server while that probe is still dialling the OLD
	// address.
	edited := *m
	edited.URL = "https://edited.example.com/mcp"
	if err := svc.MCP().UpdateMcpServer(&edited); err != nil {
		t.Fatalf("edit server mid-probe: %v", err)
	}

	second := runTestHandler(h, m.ID)
	// Same guard as the previous test, and it doubles as this test's
	// synchronization point: the second request cannot answer while the probe
	// holding the lock has published nothing, so once this window passes it
	// is parked in the wait rather than racing to claim the lock itself.
	select {
	case o := <-second:
		t.Fatalf("the contended request answered before the in-flight probe published anything: "+
			"%s (err=%v)", o.rec.Body.String(), o.err)
	case <-time.After(200 * time.Millisecond):
	}

	close(release)
	recvOutcome(t, first, "the probing request")
	secondRec := recvOutcome(t, second, "the waiting request")

	body := decodeBody(t, secondRec)
	if body["config_changed"] != true {
		t.Fatalf("config_changed = %v, want true — the committed observation was produced "+
			"with the pre-edit config, so it is not an answer about what is on screen. body=%s",
			body["config_changed"], secondRec.Body.String())
	}
}

// TestTest_WaitBudgetExpiryReportsProbingWithoutDialling pins the other exit
// from the wait: the in-flight probe publishes nothing within the budget (a
// stdio probe can legitimately run for ~125s). The request must give up on
// its own rather than hang for the probe's whole lifetime, must not start a
// competing dial, and must not fall back to the persisted row.
func TestTest_WaitBudgetExpiryReportsProbingWithoutDialling(t *testing.T) {
	svc := mcpTestSvc(t)
	m := createProbeServer(t, svc)
	seedCachedSuccess(t, svc, m.ID)
	mustHoldProbeLock(t, svc, m.ID)

	var dials atomic.Int64
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dials.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(freshProbeBody))
	}))
	defer agent.Close()

	restore := probeWaitBudget
	probeWaitBudget = 60 * time.Millisecond
	defer func() { probeWaitBudget = restore }()

	h := NewMCPHandler(svc, NewTicketStore(time.Minute), NewRunTokenStore(time.Minute), agent.URL)
	c, rec := testEcho(http.MethodPost, fmt.Sprintf("%d", m.ID), "u1")
	start := time.Now()
	if err := h.Test(c); err != nil {
		t.Fatalf("Test: %v", err)
	}
	elapsed := time.Since(start)

	if elapsed < probeWaitBudget {
		t.Fatalf("returned after %v, before the %v budget was spent — the request must actually "+
			"wait for the in-flight probe, not answer immediately", elapsed, probeWaitBudget)
	}
	body := decodeBody(t, rec)
	if body["probing"] != true || body["error_key"] != "probe_in_progress" {
		t.Fatalf("budget expiry must report an in-flight probe, got %s", rec.Body.String())
	}
	if _, present := body["tools"]; present {
		t.Fatalf("budget expiry attached the cached tool list: %s", rec.Body.String())
	}
	if n := dials.Load(); n != 0 {
		t.Fatalf("gave up waiting and then dialled anyway (%d times) — two probes of one server "+
			"is exactly what the single-flight lock prevents", n)
	}
}

// isClosed reports whether ch has been closed, without blocking.
func isClosed(ch <-chan struct{}) bool {
	select {
	case <-ch:
		return true
	default:
		return false
	}
}

// TestProbeWaiterRegistryHandsOutAFreshChannelPerProbe pins the registry rule
// the freshness contract rests on: being woken must mean "a probe published
// something just now". If a broadcast left its closed channel registered, the
// next /test request would take the woken path instantly, without any probe
// having run, and report the persisted row as a fresh result — the very
// defect the wait exists to remove.
func TestProbeWaiterRegistryHandsOutAFreshChannelPerProbe(t *testing.T) {
	h := NewMCPHandler(nil, nil, nil, "")

	first := h.subscribeProbe(7)
	alsoFirst := h.subscribeProbe(7)
	other := h.subscribeProbe(8)

	if isClosed(first) || isClosed(alsoFirst) {
		t.Fatal("a fresh subscription must block until a probe publishes")
	}

	h.broadcastProbeDone(7)

	if !isClosed(first) || !isClosed(alsoFirst) {
		t.Fatal("one broadcast must wake every waiter for that server — closing the shared " +
			"channel is what makes it a broadcast rather than a hand-off to one waiter")
	}
	if isClosed(other) {
		t.Fatal("a broadcast for server 7 woke server 8's waiter")
	}

	next := h.subscribeProbe(7)
	if isClosed(next) {
		t.Fatal("the request after a finished probe was handed the already-closed channel: it " +
			"would skip the wait entirely and answer from the persisted row without any probe " +
			"having run")
	}

	// Nobody is waiting for 9, and the next subscriber for it must still get a
	// working channel — the background TTL refresh broadcasts on every probe,
	// most of the time with no waiter registered at all.
	h.broadcastProbeDone(9)
	if isClosed(h.subscribeProbe(9)) {
		t.Fatal("a broadcast with no waiters poisoned the next subscription")
	}
}

// TestTest_WaiterIsWokenByTheBackgroundTTLRefresh is the collision this whole
// change is about, end to end. The agent's TTL self-check probes on its own
// schedule — with SCHEMA_TTL_MIN clamping the TTL to 60s, near-continuously —
// so the probe a user's "test connection" click collides with is usually that
// background refresh, not another click. The click must ride along with it and
// report its result, not the row that refresh is about to replace.
func TestTest_WaiterIsWokenByTheBackgroundTTLRefresh(t *testing.T) {
	svc, db := mcpRuntimeTestSvc(t)
	ts := NewTicketStore(time.Minute)

	release := make(chan struct{})
	probeStarted := make(chan struct{})
	var dials atomic.Int64
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if dials.Add(1) == 1 {
			close(probeStarted)
		}
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(freshProbeBody))
	}))
	defer agent.Close()

	h := NewMCPHandler(svc, ts, NewRunTokenStore(time.Minute), agent.URL)
	m := createProbeServer(t, svc)
	seedCachedSuccess(t, svc, m.ID)
	// Expire the listing so the self-check fires (SaveSuccess always stamps
	// listed_at with now, so backdate the column directly).
	if _, err := db.Exec(`UPDATE mcp_server_runtime SET listed_at=? WHERE server_id=?`,
		time.Now().Unix()-1000, m.ID); err != nil {
		t.Fatalf("backdate listed_at: %v", err)
	}

	// The agent starts a run: Runtime ships the stale listing and kicks off
	// the background refresh without waiting for it.
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.Header.Set("X-Agent-MCP-Ticket", ts.Mint("u1"))
	if err := h.Runtime(echo.New().NewContext(req, httptest.NewRecorder())); err != nil {
		t.Fatalf("runtime: %v", err)
	}
	select {
	case <-probeStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("the TTL self-check never reached the agent")
	}

	// Now the user clicks "test connection".
	clicked := runTestHandler(h, m.ID)
	select {
	case o := <-clicked:
		t.Fatalf("the click answered while the background refresh was still dialling: %s (err=%v)",
			o.rec.Body.String(), o.err)
	case <-time.After(200 * time.Millisecond):
	}

	close(release)
	rec := recvOutcome(t, clicked, "the test-connection click")
	body := decodeBody(t, rec)
	if body["ok"] != true {
		t.Fatalf("ok = %v, want true. body=%s", body["ok"], rec.Body.String())
	}
	tools, _ := body["tools"].([]any)
	if len(tools) != 1 || tools[0] != "fresh_tool" {
		t.Fatalf("tools = %v, want [fresh_tool] — the click reported the pre-refresh row. body=%s",
			body["tools"], rec.Body.String())
	}
	if n := dials.Load(); n != 1 {
		t.Fatalf("the agent was dialled %d times, want 1: the click must ride along with the "+
			"background refresh instead of starting a competing probe", n)
	}
}
