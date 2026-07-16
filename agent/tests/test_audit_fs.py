import asyncio
import json
import audit as A
from fs import ops


class _Sink:
    def __init__(self):
        self.events: list[dict] = []

    async def put(self, ev):
        self.events.append(ev)


def test_fs_change_audited(tmp_path):
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    sink = _Sink()
    ctx = {"session_id": "s1", "user_id": "u1", "run_id": "r1", "sink": sink}
    asyncio.run(ops._emit_staged(ctx, 1, "delete_file", "/DATA/x.txt"))
    recs = [json.loads(l) for l in (tmp_path / "audit.log").read_text().splitlines()]
    ev = [r for r in recs if r["event"] == "fs_change"]
    assert ev and ev[-1]["op"] == "delete_file" and ev[-1]["path"] == "/DATA/x.txt"
    assert ev[-1]["session_id"] == "s1"
    # existing staged_change SSE behavior must be untouched
    assert sink.events and sink.events[0]["type"] == "staged_change"


def test_fs_change_audited_with_dst_path(tmp_path):
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    sink = _Sink()
    ctx = {"session_id": "s1", "user_id": "u1", "run_id": "r1", "sink": sink}
    asyncio.run(ops._emit_staged(ctx, 2, "rename", "/DATA/a.txt", dst_path="/DATA/b.txt"))
    recs = [json.loads(l) for l in (tmp_path / "audit.log").read_text().splitlines()]
    ev = [r for r in recs if r["event"] == "fs_change"]
    assert ev and ev[-1]["op"] == "rename"
    assert ev[-1]["path"] == "/DATA/a.txt"
    assert ev[-1]["dst_path"] == "/DATA/b.txt"
