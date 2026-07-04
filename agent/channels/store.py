"""SQLite accessors for channel instances, pairing codes, bindings and
chat->session mapping. Pairing codes follow the mcp_tokens playbook:
store sha256 only, one-time use, short TTL. Timestamps are milliseconds
except sessions rows (seconds, matching the rest of the sessions table)."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid

PAIRING_TTL_MS = 10 * 60 * 1000


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# -- instances ---------------------------------------------------------------

def create_instance(conn: sqlite3.Connection, channel_type: str, name: str,
                    config: dict, created_by: str, now_ms: int) -> dict:
    iid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO channel_instances "
        "(id, channel_type, name, config_json, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (iid, channel_type, name or "", json.dumps(config), str(created_by),
         now_ms, now_ms))
    conn.commit()
    return get_instance(conn, iid)


def get_instance(conn, instance_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM channel_instances WHERE id=?",
                       (instance_id,)).fetchone()
    return dict(row) if row else None


def list_instances(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM channel_instances ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


def set_instance_enabled(conn, instance_id: str, enabled: bool,
                         now_ms: int) -> bool:
    cur = conn.execute(
        "UPDATE channel_instances SET enabled=?, updated_at=? WHERE id=?",
        (1 if enabled else 0, now_ms, instance_id))
    conn.commit()
    return cur.rowcount > 0


def delete_instance(conn, instance_id: str) -> bool:
    cur = conn.execute("DELETE FROM channel_instances WHERE id=?",
                       (instance_id,))
    conn.execute("DELETE FROM channel_bindings WHERE instance_id=?",
                 (instance_id,))
    conn.execute("DELETE FROM channel_pairing_codes WHERE instance_id=?",
                 (instance_id,))
    conn.execute("DELETE FROM channel_chats WHERE instance_id=?",
                 (instance_id,))
    conn.commit()
    return cur.rowcount > 0


# -- pairing -----------------------------------------------------------------

def create_pairing_code(conn, instance_id: str, user_id: str,
                        now_ms: int) -> tuple[str, int]:
    code = f"{secrets.randbelow(10**8):08d}"
    expires = now_ms + PAIRING_TTL_MS
    conn.execute(
        "INSERT INTO channel_pairing_codes "
        "(id, code_hash, instance_id, user_id, expires_at) VALUES (?,?,?,?,?)",
        (uuid.uuid4().hex, _hash(code), instance_id, str(user_id), expires))
    conn.commit()
    return code, expires


def redeem_pairing_code(conn, instance_id: str, code: str,
                        external_user_id: str, external_username: str | None,
                        now_ms: int) -> dict | None:
    row = conn.execute(
        "SELECT id, user_id FROM channel_pairing_codes "
        "WHERE code_hash=? AND instance_id=? AND used_at IS NULL AND expires_at>?",
        (_hash(code), instance_id, now_ms)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE channel_pairing_codes SET used_at=? WHERE id=?",
                 (now_ms, row["id"]))
    conn.execute(
        "INSERT INTO channel_bindings "
        "(id, instance_id, external_user_id, external_username, user_id, created_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(instance_id, external_user_id) DO UPDATE SET "
        "user_id=excluded.user_id, external_username=excluded.external_username, "
        "revoked=0",
        (uuid.uuid4().hex, instance_id, str(external_user_id),
         external_username, str(row["user_id"]), now_ms))
    conn.commit()
    return get_binding(conn, instance_id, external_user_id)


# -- bindings ----------------------------------------------------------------

def get_binding(conn, instance_id: str, external_user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM channel_bindings "
        "WHERE instance_id=? AND external_user_id=? AND revoked=0",
        (instance_id, str(external_user_id))).fetchone()
    return dict(row) if row else None


def list_bindings_for_user(conn, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM channel_bindings WHERE user_id=? AND revoked=0 "
        "ORDER BY created_at DESC", (str(user_id),)).fetchall()
    return [dict(r) for r in rows]


def revoke_binding(conn, user_id: str, binding_id: str) -> bool:
    cur = conn.execute(
        "UPDATE channel_bindings SET revoked=1 "
        "WHERE id=? AND user_id=? AND revoked=0",
        (binding_id, str(user_id)))
    conn.commit()
    return cur.rowcount > 0


def set_binding_model(conn, user_id: str, binding_id: str,
                      model: str) -> bool:
    cur = conn.execute(
        "UPDATE channel_bindings SET default_model=? "
        "WHERE id=? AND user_id=? AND revoked=0",
        (model, binding_id, str(user_id)))
    conn.commit()
    return cur.rowcount > 0


# -- chats & sessions --------------------------------------------------------

def get_chat(conn, instance_id: str, external_chat_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM channel_chats WHERE instance_id=? AND external_chat_id=?",
        (instance_id, str(external_chat_id))).fetchone()
    return dict(row) if row else None


def upsert_chat(conn, instance_id: str, external_chat_id: str,
                binding_id: str, session_id: str, now_ms: int) -> None:
    conn.execute(
        "INSERT INTO channel_chats "
        "(id, instance_id, external_chat_id, binding_id, session_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(instance_id, external_chat_id) DO UPDATE SET "
        "binding_id=excluded.binding_id, session_id=excluded.session_id, "
        "updated_at=excluded.updated_at",
        (uuid.uuid4().hex, instance_id, str(external_chat_id), binding_id,
         session_id, now_ms, now_ms))
    conn.commit()


def create_channel_session(conn, user_id: str, source: str) -> str:
    session_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, created_at, updated_at, "
        "agent_type, source) VALUES (?,?,?,?,?,?,?)",
        (session_id, str(user_id), None, now, now, "general", source))
    conn.commit()
    return session_id
