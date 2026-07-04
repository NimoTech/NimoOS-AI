import os
import db as db_module
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
