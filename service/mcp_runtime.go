package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"time"
)

// McpServerRuntime is the observed state of one MCP server: identity card,
// tool list, protocol era and health — everything Go learns by probing the
// server and everything downstream (the runtime endpoint, the approval
// gates, the agent's zero-network start) reads back from here.
type McpServerRuntime struct {
	ServerID      int64
	ServerName    string
	ServerTitle   string
	ServerVersion string
	Handle        string
	Instructions  string
	Summary       string
	ToolsJSON     string
	ListedAt      int64
	TTLSec        int64
	ConfigFP      string
	IdentityFP    string
	ProtocolMode  string
	ProtocolEra   string
	ProbeState    string
	LastOkAt      int64
	LastErrorAt   int64
	LastError     string
	LastErrorKey  string
	FailStreak    int
	CooldownUntil int64
	EmptyStreak   int
}

// ToolMeta is one element of tools_json. SchemaHash and DescHash are
// computed only in Python and stored verbatim; Go never recomputes or
// normalizes them.
type ToolMeta struct {
	Name       string `json:"name"`
	SchemaHash string `json:"schema_hash"`
	DescHash   string `json:"desc_hash"`
}

type mcpRuntimeService struct{ db *sql.DB }

const mcpRuntimeCols = `server_id, server_name, server_title, server_version, handle, instructions, summary,
	tools_json, listed_at, ttl_sec, config_fp, identity_fp, protocol_mode, protocol_era,
	probe_state, last_ok_at, last_error_at, last_error, last_error_key, fail_streak,
	cooldown_until, empty_streak`

func scanMcpRuntime(sc interface{ Scan(...any) error }) (*McpServerRuntime, error) {
	r := &McpServerRuntime{}
	if err := sc.Scan(&r.ServerID, &r.ServerName, &r.ServerTitle, &r.ServerVersion, &r.Handle,
		&r.Instructions, &r.Summary, &r.ToolsJSON, &r.ListedAt, &r.TTLSec, &r.ConfigFP,
		&r.IdentityFP, &r.ProtocolMode, &r.ProtocolEra, &r.ProbeState, &r.LastOkAt,
		&r.LastErrorAt, &r.LastError, &r.LastErrorKey, &r.FailStreak, &r.CooldownUntil,
		&r.EmptyStreak); err != nil {
		return nil, err
	}
	return r, nil
}

// Get returns the runtime row for serverID, or (nil, nil) if the server has
// never been probed (a server added before this feature existed, or one
// that hasn't had its first probe yet). This is a normal, expected state,
// not an error — callers must treat a nil result as "no observation yet"
// rather than propagate sql.ErrNoRows.
func (s *mcpRuntimeService) Get(serverID int64) (*McpServerRuntime, error) {
	row := s.db.QueryRow(`SELECT `+mcpRuntimeCols+` FROM mcp_server_runtime WHERE server_id=?`, serverID)
	r, err := scanMcpRuntime(row)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return r, nil
}

// List returns every runtime row belonging to userID, keyed by server_id.
// Servers without a runtime row yet are simply absent from the map (same
// "no observation yet" convention as Get).
func (s *mcpRuntimeService) List(userID string) (map[int64]*McpServerRuntime, error) {
	rows, err := s.db.Query(`
		SELECT `+mcpRuntimeCols+`
		FROM mcp_server_runtime
		JOIN mcp_servers ON mcp_servers.id = mcp_server_runtime.server_id AND mcp_servers.user_id = ?`,
		userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]*McpServerRuntime{}
	for rows.Next() {
		r, err := scanMcpRuntime(rows)
		if err != nil {
			return nil, err
		}
		out[r.ServerID] = r
	}
	return out, rows.Err()
}

// MarkProbing is the probe's single-flight lock: claim the lock before doing
// any work. Returns false if a probe is already running and the caller
// should give up. The UPSERT's WHERE clause makes "claiming the lock" a
// single atomic write — no extra mutex needed.
func (s *mcpRuntimeService) MarkProbing(serverID int64) (bool, error) {
	res, err := s.db.Exec(`
		INSERT INTO mcp_server_runtime (server_id, probe_state) VALUES (?, 'probing')
		ON CONFLICT(server_id) DO UPDATE SET probe_state='probing'
		WHERE mcp_server_runtime.probe_state <> 'probing'`, serverID)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

// SaveSuccess persists one successful probe. Three invariants live here:
//  1. listed_at advances on every success — Python's in-memory cache is
//     keyed on it; if it doesn't advance, a changed tool description can
//     never reach the model.
//  2. A successful but empty listing does not overwrite the tool list; it
//     only accumulates empty_streak. Only two consecutive empty listings
//     actually clear it — a single blip must not wipe out the user's tool
//     list and their approvals for it.
//  3. The last_seen_at heartbeat only advances for tools that appeared in
//     this listing; it never regresses just because "we didn't see it this
//     time".
func (s *mcpRuntimeService) SaveSuccess(r *McpServerRuntime, tools []ToolMeta, schemasJSON string) error {
	now := time.Now().Unix()

	// The database is opened with _journal_mode=WAL (see NewDB). A plain
	// db.Begin() is DEFERRED: its first statement below is the SELECT of
	// empty_streak, which only takes a read snapshot. If any other
	// connection commits a write before this transaction's own first write
	// statement runs, SQLite refuses to upgrade that read snapshot and
	// returns SQLITE_BUSY_SNAPSHOT — an error the busy-timeout handler does
	// NOT retry, so the whole probe result would be silently discarded
	// (and, combined with the single-flight lock, that server's probe_state
	// would stay stuck at 'probing' since neither SaveSuccess nor its
	// caller would ever reach the code that clears it).
	//
	// The single-flight lock in MarkProbing does not protect against this:
	// it only serializes probes of the *same* server, but concurrent
	// probes of *different* servers still write the same ai.db file.
	//
	// Fix: check out a single dedicated connection and issue a literal
	// BEGIN IMMEDIATE on it, so the write lock is acquired up front. Any
	// contention then surfaces as ordinary SQLITE_BUSY, which go-sqlite3's
	// busy handler does retry. We deliberately do NOT add _txlock=immediate
	// to the shared DSN in NewDB — that would change transaction semantics
	// for every other caller of this *sql.DB (sessions, provider_models,
	// ...), not just this one read-then-write transaction.
	ctx := context.Background()
	conn, err := s.db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()

	if _, err = conn.ExecContext(ctx, `BEGIN IMMEDIATE`); err != nil {
		return err
	}
	committed := false
	defer func() {
		if !committed {
			_, _ = conn.ExecContext(ctx, `ROLLBACK`)
		}
	}()

	empty := len(tools) == 0
	var prevEmpty int
	conn.QueryRowContext(ctx, `SELECT empty_streak FROM mcp_server_runtime WHERE server_id=?`, r.ServerID).Scan(&prevEmpty)

	writeList := !empty || prevEmpty+1 >= 2 // only clear on two consecutive empty listings
	newEmptyStreak := 0
	if empty {
		newEmptyStreak = prevEmpty + 1
	}

	toolsJSON := "[]"
	if b, e := json.Marshal(tools); e == nil {
		toolsJSON = string(b)
	}

	// Many columns: UPSERT writes them out one by one to avoid any
	// dependency on SELECT * column order.
	if writeList {
		if _, err = conn.ExecContext(ctx, `
			INSERT INTO mcp_server_runtime
			 (server_id,server_name,server_title,server_version,handle,instructions,summary,
			  tools_json,listed_at,ttl_sec,config_fp,identity_fp,protocol_mode,protocol_era,
			  probe_state,last_ok_at,last_error,last_error_key,fail_streak,cooldown_until,empty_streak)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ok',?,'','',0,0,?)
			ON CONFLICT(server_id) DO UPDATE SET
			 server_name=excluded.server_name, server_title=excluded.server_title,
			 server_version=excluded.server_version, handle=excluded.handle,
			 instructions=excluded.instructions, summary=excluded.summary,
			 tools_json=excluded.tools_json, listed_at=excluded.listed_at,
			 ttl_sec=excluded.ttl_sec, config_fp=excluded.config_fp,
			 identity_fp=excluded.identity_fp, protocol_mode=excluded.protocol_mode,
			 protocol_era=excluded.protocol_era, probe_state='ok', last_ok_at=excluded.last_ok_at,
			 last_error='', last_error_key='', fail_streak=0, cooldown_until=0,
			 empty_streak=excluded.empty_streak`,
			r.ServerID, r.ServerName, r.ServerTitle, r.ServerVersion, r.Handle,
			r.Instructions, r.Summary, toolsJSON, now, r.TTLSec, r.ConfigFP, r.IdentityFP,
			r.ProtocolMode, r.ProtocolEra, now, newEmptyStreak); err != nil {
			return err
		}
		if _, err = conn.ExecContext(ctx, `
			INSERT INTO mcp_server_schemas (server_id, listed_at, schemas_json) VALUES (?,?,?)
			ON CONFLICT(server_id) DO UPDATE SET listed_at=excluded.listed_at,
			 schemas_json=excluded.schemas_json`, r.ServerID, now, schemasJSON); err != nil {
			return err
		}
	} else {
		// Empty listing, but the first one (or the server has no runtime
		// row at all yet — e.g. its very first probe happens to see zero
		// tools): only record success and empty_streak, and leave the
		// tool list, schemas and listed_at untouched. INSERT-or-UPDATE so
		// this converges correctly whether or not a row already exists —
		// a bare UPDATE would silently affect zero rows and record
		// nothing when there is no pre-existing row (e.g. if MarkProbing
		// was skipped or its row was never created).
		if _, err = conn.ExecContext(ctx, `
			INSERT INTO mcp_server_runtime (server_id, probe_state, last_ok_at, empty_streak)
			VALUES (?, 'ok', ?, ?)
			ON CONFLICT(server_id) DO UPDATE SET probe_state='ok', last_ok_at=excluded.last_ok_at,
			 last_error='', last_error_key='', fail_streak=0, cooldown_until=0,
			 empty_streak=excluded.empty_streak`,
			r.ServerID, now, newEmptyStreak); err != nil {
			return err
		}
	}

	// Heartbeat: only advance for tools that appeared in this listing. An
	// empty listing triggers nothing (the loop body never runs).
	for _, tl := range tools {
		if _, err = conn.ExecContext(ctx, `UPDATE mcp_tool_approvals SET last_seen_at=?
			WHERE server_id=? AND tool_name=?`, now, r.ServerID, tl.Name); err != nil {
			return err
		}
	}
	// The service-level '*' row follows any successful, non-empty probe.
	if !empty {
		if _, err = conn.ExecContext(ctx, `UPDATE mcp_tool_approvals SET last_seen_at=?
			WHERE server_id=? AND tool_name='*'`, now, r.ServerID); err != nil {
			return err
		}
	}
	if _, err = conn.ExecContext(ctx, `COMMIT`); err != nil {
		return err
	}
	committed = true
	return nil
}

// SaveFailure never touches last_seen_at: otherwise a server down for a
// week would let every one of its tools go stale, and re-ask the user for
// approval on all of them once it recovers.
func (s *mcpRuntimeService) SaveFailure(serverID int64, errKey, errMsg string) error {
	now := time.Now().Unix()
	var streak int
	s.db.QueryRow(`SELECT fail_streak FROM mcp_server_runtime WHERE server_id=?`, serverID).Scan(&streak)
	streak++
	cooldown := now + int64(Backoff(streak).Seconds())
	_, err := s.db.Exec(`
		INSERT INTO mcp_server_runtime
		 (server_id,probe_state,last_error_at,last_error,last_error_key,fail_streak,cooldown_until)
		VALUES (?,'failed',?,?,?,?,?)
		ON CONFLICT(server_id) DO UPDATE SET probe_state='failed', last_error_at=excluded.last_error_at,
		 last_error=excluded.last_error, last_error_key=excluded.last_error_key,
		 fail_streak=excluded.fail_streak, cooldown_until=excluded.cooldown_until`,
		serverID, now, errMsg, errKey, streak, cooldown)
	return err
}

// GetSchemas returns the stored schema bodies for serverID. If the server
// has never had a successful non-empty probe, it returns (0, "[]", nil) —
// the same "no observation yet" convention as Get, since callers only need
// a safe default to fall back on, not an error path.
func (s *mcpRuntimeService) GetSchemas(serverID int64) (int64, string, error) {
	var listedAt int64
	var schemasJSON string
	err := s.db.QueryRow(`SELECT listed_at, schemas_json FROM mcp_server_schemas WHERE server_id=?`, serverID).
		Scan(&listedAt, &schemasJSON)
	if err == sql.ErrNoRows {
		return 0, "[]", nil
	}
	if err != nil {
		return 0, "", err
	}
	return listedAt, schemasJSON, nil
}
