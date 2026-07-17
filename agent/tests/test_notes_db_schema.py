"""M1 新表存在性 + 关键列。"""
import db as db_module


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_notes_tables_exist(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    assert {"id", "user_id", "path", "title", "description", "type",
            "status", "content_hash", "source_refs_json", "created_by",
            "revision", "created_at", "updated_at", "deleted_at",
            "extraction_status", "extracted_at",
            "content_hash_at_extraction"} <= _cols(conn, "notes")
    assert {"src_note_id", "dst_kind", "dst_ref", "anchor_text"} \
        <= _cols(conn, "note_links")
    assert {"id", "user_id", "name", "type", "aliases_json", "description",
            "qdrant_point_id", "created_at", "updated_at",
            "deleted_at"} <= _cols(conn, "entities")
    assert {"id", "user_id", "src_entity", "dst_entity", "rel_type",
            "weight", "description", "source_refs_json"} \
        <= _cols(conn, "edges")
    assert {"entity_id", "note_id", "chunk_ref"} <= _cols(conn, "mentions")


def test_notes_indexes_exist(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"),
                             snapshots_root=str(tmp_path / "snaps"))
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_notes_user_status" in names
    assert "idx_mentions_entity" in names
