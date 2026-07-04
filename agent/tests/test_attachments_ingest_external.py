import os, sqlite3
import db as db_module
from attachments.ingest import ingest_external


def _conn(tmp):
    return db_module.init_db(str(tmp / "t.db"), snapshots_root=str(tmp / "snaps"))


def test_ingest_external_symlinks_and_reads(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) "
                 "VALUES ('s1','u1',1,1)"); conn.commit()
    data_root = str(tmp_path / "agentdata")
    # a real file living OUTSIDE the session store (stands in for /DATA)
    real = tmp_path / "DATA" / "Downloads" / "telegram" / "hi.txt"
    real.parent.mkdir(parents=True); real.write_text("hello-real")
    aid = ingest_external(conn, data_root, "s1", real_path=str(real), filename="hi.txt")
    assert aid.startswith("att_")
    row = conn.execute("SELECT filename,kind,size_bytes,rel_path FROM attachments "
                       "WHERE id=?", (aid,)).fetchone()
    assert row["filename"] == "hi.txt" and row["size_bytes"] == len("hello-real")
    # symlink created in the session store, pointing at the real /DATA file
    link = os.path.join(data_root, "sessions", "s1", "attachments", row["rel_path"])
    assert os.path.islink(link) and os.path.realpath(link) == os.path.realpath(str(real))
    # existing read pattern (join then open) transparently follows the symlink
    with open(link) as f:
        assert f.read() == "hello-real"


def test_ingest_external_missing_file_raises(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) "
                 "VALUES ('s1','u1',1,1)"); conn.commit()
    import pytest
    with pytest.raises(FileNotFoundError):
        ingest_external(conn, str(tmp_path / "d"), "s1",
                        real_path=str(tmp_path / "nope.txt"), filename="nope.txt")
