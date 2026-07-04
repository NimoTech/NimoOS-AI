import os
import db as db_module
import channels.inbound as inbound_mod
from channels.inbound import save_and_ingest
from channels.model import InboundAttachment


def _mk_tmp(tmp, name, data=b"x"):
    p = tmp / name; p.write_bytes(data); return str(p)


def test_save_ingest_moves_to_download_dir_and_cleans_tmp(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"), snapshots_root=str(tmp_path / "s"))
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES ('s','u',1,1)"); conn.commit()
    ddir = str(tmp_path / "DATA" / "Downloads" / "telegram")
    t1 = _mk_tmp(tmp_path, "a.txt", b"aaa")
    atts = [InboundAttachment(filename="a.txt", mime="text/plain", tmp_path=t1, size=3)]
    ids, skipped = save_and_ingest(conn, str(tmp_path / "ad"), "s", ddir, atts,
                                   max_file=100, max_total=1000, max_count=10)
    assert len(ids) == 1 and skipped == []
    assert os.path.isfile(os.path.join(ddir, "a.txt"))   # 真文件落 download_dir
    assert not os.path.exists(t1)                        # tmp 清理


def test_limits_skip_and_collision_rename(tmp_path):
    conn = db_module.init_db(str(tmp_path / "t.db"), snapshots_root=str(tmp_path / "s"))
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES ('s','u',1,1)"); conn.commit()
    ddir = str(tmp_path / "DATA")
    big = _mk_tmp(tmp_path, "big.bin", b"x" * 50)
    ok1 = _mk_tmp(tmp_path, "n.txt", b"1"); ok2 = _mk_tmp(tmp_path, "n2.txt", b"2")
    atts = [InboundAttachment("big.bin", "application/octet-stream", big, 50),
            InboundAttachment("n.txt", "text/plain", ok1, 1),
            InboundAttachment("n.txt", "text/plain", ok2, 1)]   # 同名
    ids, skipped = save_and_ingest(conn, str(tmp_path / "ad"), "s", ddir, atts,
                                   max_file=10, max_total=1000, max_count=10)
    assert "big.bin" in skipped and len(ids) == 2
    names = sorted(os.listdir(ddir))
    assert names == ["n (1).txt", "n.txt"]               # 重名追加后缀


def test_cap_uses_real_bytes_not_claimed_size(tmp_path):
    """A malicious/buggy adapter under-reports att.size to bypass the cap;
    the cap must be enforced against the real on-disk tmp file size."""
    conn = db_module.init_db(str(tmp_path / "t.db"), snapshots_root=str(tmp_path / "s"))
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES ('s','u',1,1)"); conn.commit()
    ddir = str(tmp_path / "DATA")
    lying = _mk_tmp(tmp_path, "lying.bin", b"x" * 50)   # real 50 bytes
    atts = [InboundAttachment("lying.bin", "application/octet-stream", lying, 1)]  # claims size=1
    ids, skipped = save_and_ingest(conn, str(tmp_path / "ad"), "s", ddir, atts,
                                   max_file=10, max_total=1000, max_count=10)
    assert ids == [] and skipped == ["lying.bin"]
    assert not os.path.exists(lying)                     # tmp still cleaned up


def test_per_attachment_error_isolated(tmp_path, monkeypatch):
    """If one attachment fails to move/ingest, the others must still succeed
    and all tmp files must be cleaned up (no whole-batch abort)."""
    conn = db_module.init_db(str(tmp_path / "t.db"), snapshots_root=str(tmp_path / "s"))
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES ('s','u',1,1)"); conn.commit()
    ddir = str(tmp_path / "DATA")
    t1 = _mk_tmp(tmp_path, "a.txt", b"1")
    t2 = _mk_tmp(tmp_path, "b.txt", b"2")
    t3 = _mk_tmp(tmp_path, "c.txt", b"3")
    atts = [InboundAttachment("a.txt", "text/plain", t1, 1),
            InboundAttachment("b.txt", "text/plain", t2, 1),
            InboundAttachment("c.txt", "text/plain", t3, 1)]

    real_ingest = inbound_mod.ingest_external
    calls = {"n": 0}

    def flaky_ingest(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(inbound_mod, "ingest_external", flaky_ingest)
    ids, skipped = save_and_ingest(conn, str(tmp_path / "ad"), "s", ddir, atts,
                                   max_file=100, max_total=1000, max_count=10)
    assert len(ids) == 2 and skipped == ["b.txt"]
    assert not os.path.exists(t1) and not os.path.exists(t2) and not os.path.exists(t3)


def test_getsize_failure_isolated(tmp_path):
    """If os.path.getsize() raises for one attachment's tmp file (e.g. it
    vanished / was never created), that attachment is skipped but the
    others before and after it must still be saved and ingested."""
    conn = db_module.init_db(str(tmp_path / "t.db"), snapshots_root=str(tmp_path / "s"))
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES ('s','u',1,1)"); conn.commit()
    ddir = str(tmp_path / "DATA")
    t1 = _mk_tmp(tmp_path, "a.txt", b"1")
    bogus = str(tmp_path / "does-not-exist.bin")   # getsize() will raise FileNotFoundError
    t3 = _mk_tmp(tmp_path, "c.txt", b"3")
    atts = [InboundAttachment("a.txt", "text/plain", t1, 1),
            InboundAttachment("bad.bin", "application/octet-stream", bogus, 1),
            InboundAttachment("c.txt", "text/plain", t3, 1)]
    ids, skipped = save_and_ingest(conn, str(tmp_path / "ad"), "s", ddir, atts,
                                   max_file=100, max_total=1000, max_count=10)
    assert len(ids) == 2 and skipped == ["bad.bin"]
    assert os.path.isfile(os.path.join(ddir, "a.txt"))
    assert os.path.isfile(os.path.join(ddir, "c.txt"))
    assert not os.path.exists(t1) and not os.path.exists(t3)


def test_running_total_not_charged_on_ingest_failure(tmp_path, monkeypatch):
    """A move-succeeds/ingest-fails attachment must not consume the
    per-message total-size budget: a later attachment that only fits if the
    failed one's bytes are NOT counted must still be accepted."""
    conn = db_module.init_db(str(tmp_path / "t.db"), snapshots_root=str(tmp_path / "s"))
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES ('s','u',1,1)"); conn.commit()
    ddir = str(tmp_path / "DATA")
    t1 = _mk_tmp(tmp_path, "a.txt", b"x" * 6)   # fails ingest, 6 bytes
    t2 = _mk_tmp(tmp_path, "b.txt", b"y" * 6)   # only fits if a.txt's 6 bytes weren't charged

    real_ingest = inbound_mod.ingest_external
    calls = {"n": 0}

    def flaky_ingest(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(inbound_mod, "ingest_external", flaky_ingest)
    atts = [InboundAttachment("a.txt", "text/plain", t1, 6),
            InboundAttachment("b.txt", "text/plain", t2, 6)]
    ids, skipped = save_and_ingest(conn, str(tmp_path / "ad"), "s", ddir, atts,
                                   max_file=100, max_total=10, max_count=10)
    assert skipped == ["a.txt"]
    assert len(ids) == 1
    assert os.path.isfile(os.path.join(ddir, "b.txt"))
    assert not os.path.exists(t1) and not os.path.exists(t2)
