import asyncio
import json
import db as dbmod
import audit as A
from skills import shell


class _Mgr:
    def __init__(self, grant): self._g=grant; self.registered=[]
    def register(self,sid,a,d,c): self.registered.append((a,c)); return "c1"
    async def wait(self,cid): return self._g
    def consume_remember(self,cid): return False


class _Sink:
    async def put(self, ev): pass


def _setup(monkeypatch, tmp_path, mgr):
    A.set_audit_path_for_test(str(tmp_path / "audit.log"))
    conn = dbmod.init_db(":memory:")
    conn.execute("INSERT INTO sessions (id,user_id,created_at,updated_at) VALUES ('s1','u1',0,0)")
    conn.commit()
    shell.SESSION_ID_VAR.set("s1"); shell.DB_VAR.set(conn)
    shell.CONFIRM_MGR_VAR.set(mgr); shell.EVENT_QUEUE_VAR.set(_Sink())
    monkeypatch.setattr("shell_guard.backstop.prepare_backstop",
        lambda paths, trash_root=None: __import__("shell_guard.backstop",
        fromlist=["BackstopResult"]).BackstopResult("none","",False,""))


def test_refused_dangerous_is_audited(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, _Mgr(grant=False))
    asyncio.run(shell._guard_command("rm -rf /DATA/x"))
    recs = [json.loads(l) for l in (tmp_path/"audit.log").read_text().splitlines()]
    ev = [r for r in recs if r["event"] == "shell_command"]
    assert ev and ev[-1]["command"] == "rm -rf /DATA/x"
    assert ev[-1]["level"] in ("dangerous", "protected")
    assert ev[-1]["outcome"] in ("refused_user", "refused_unattended")


def test_safe_command_not_audited(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, _Mgr(grant=True))
    asyncio.run(shell._guard_command("ls -la"))
    logf = tmp_path/"audit.log"
    recs = [json.loads(l) for l in logf.read_text().splitlines()] if logf.exists() else []
    assert not [r for r in recs if r["event"] == "shell_command"]
