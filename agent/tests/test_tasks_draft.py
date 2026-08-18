# NimoOS-AI/agent/tests/test_tasks_draft.py
"""M6 会话转定时任务 —— 草稿纯函数。

这里的每条断言都在守一个授权边界:前缀越短权限越大,所以归一化宁可
多留 token 也不能少留;egress 是从命令文本猜的,所以永远不进 preauth。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tasks import draft


# ---- normalize_prefix ---------------------------------------------------

def test_prefix_stops_at_three_tokens():
    assert draft.normalize_prefix("lark-cli base record create --app x") == "lark-cli base record"


def test_prefix_stops_at_flag():
    assert draft.normalize_prefix("gh pr list --limit 5") == "gh pr list"
    assert draft.normalize_prefix("date -u") == "date"


def test_prefix_stops_at_value_like_token():
    # URL 含 ':' 与 '/';路径含 '/';赋值含 '='
    assert draft.normalize_prefix("curl https://a.com") == "curl"
    assert draft.normalize_prefix("cat /DATA/x.txt") == "cat"
    assert draft.normalize_prefix("env FOO=1 bar") == "env"


def test_prefix_always_keeps_first_token():
    # 首 token 必留(否则前缀为空 = 授权一切),而且它是值形态也不终止扫描 ——
    # 停在 `/usr/bin/env` 会把 `env <任意命令>` 一并授权,比多留一个 token 危险。
    assert draft.normalize_prefix("/usr/bin/tool") == "/usr/bin/tool"
    assert draft.normalize_prefix("/usr/bin/env foo") == "/usr/bin/env foo"


def test_prefix_of_path_invoked_script_keeps_its_subcommand():
    # 停在脚本路径就等于授权它的任意参数;多留一个 token 才是收窄。
    assert (draft.normalize_prefix("/DATA/scripts/deploy.sh production")
            == "/DATA/scripts/deploy.sh production")


def test_prefix_stays_a_literal_prefix_of_the_command():
    cmd = 'lark-cli base "record two" create'
    p = draft.normalize_prefix(cmd)
    assert p == "lark-cli base"
    assert cmd.startswith(p)


def test_prefix_retreats_on_repeated_whitespace():
    assert draft.normalize_prefix("gh  pr  list") == "gh"


def test_prefix_gives_up_when_the_command_starts_with_a_quote():
    assert draft.normalize_prefix('"gh" pr list') is None


def test_prefix_rejects_compound_commands():
    for cmd in ["a && b", "a | b", "a; b", "a > f", "a `b`", "a $(b)", "a\nb"]:
        assert draft.normalize_prefix(cmd) is None, cmd


def test_prefix_rejects_unlexable_and_empty():
    assert draft.normalize_prefix("echo 'unbalanced") is None
    assert draft.normalize_prefix("   ") is None
    assert draft.normalize_prefix(None) is None


# ---- parse_mcp_call -----------------------------------------------------

def test_parse_mcp_call():
    assert draft.parse_mcp_call("mcp__my_server__search") == ("my_server", "search")
    assert draft.parse_mcp_call("mcp__s__a__b") == ("s", "a__b")
    assert draft.parse_mcp_call("run_command") is None
    assert draft.parse_mcp_call("mcp__onlyslug") is None


# ---- extract_hosts ------------------------------------------------------

def test_extract_hosts():
    assert draft.extract_hosts("curl https://open.feishu.cn/x -d @a") == ["open.feishu.cn"]
    assert draft.extract_hosts("no url here") == []
    # 端口剥掉,去重,顺序稳定
    assert draft.extract_hosts("http://a.com:8080 https://a.com/y https://b.io") == ["a.com", "b.io"]


def test_extract_hosts_rejects_non_hosts():
    assert draft.extract_hosts("(see https://a.com)") == ["a.com"]
    assert draft.extract_hosts("check https://open.feishu.cn.") == ["open.feishu.cn"]
    assert draft.extract_hosts("https://user:pass@a.com/x") == ["a.com"]
    assert draft.extract_hosts("https://[::1]:8080/x") == ["[::1]"]
    # 无方括号的 IPv6 剥端口后是残缺地址,不是主机名 —— 丢掉而不是猜
    assert draft.extract_hosts("http://2001:db8::1/x") == []


# ---- scan_history -------------------------------------------------------

def _call(name, args):
    return {"type": "function_call", "name": name, "arguments": args}


def test_scan_collects_all_four_buckets():
    history = [
        {"role": "user", "content": "帮我汇总"},
        _call("run_command", '{"command": "lark-cli base record create --app x"}'),
        _call("run_command", '{"command": "curl https://open.feishu.cn/api"}'),
        _call("mcp__my_server__search", '{"q": "x"}'),
        _call("write_file", '{"path": "/DATA/Documents/reports/a.md", "content": "x"}'),
    ]
    out = draft.scan_history(history, mcp_id_by_slug={"my_server": "srv-1"})

    assert out["preauth"]["shell"] == [
        {"kind": "prefix", "value": "lark-cli base record"},
        {"kind": "prefix", "value": "curl"},
    ]
    assert out["preauth"]["mcp_tools"] == ["srv-1::search"]
    assert out["preauth"]["fs_write"] == ["/DATA/Documents/reports"]
    # 猜出来的东西不进 preauth
    assert out["preauth"]["egress_domains"] == []
    assert out["suggested_egress"] == ["open.feishu.cn"]


def test_scan_records_evidence_for_every_rule():
    history = [_call("run_command", '{"command": "gh pr list --limit 5"}')]
    out = draft.scan_history(history, mcp_id_by_slug={})
    assert out["evidence"]["shell:gh pr list"] == "gh pr list --limit 5"


def test_scan_records_evidence_for_mcp_and_fs_rules():
    history = [
        _call("mcp__my_server__search", '{"q": "x"}'),
        _call("write_file", '{"path": "/DATA/Documents/r/a.md"}'),
    ]
    out = draft.scan_history(history, mcp_id_by_slug={"my_server": "srv-1"})
    assert out["evidence"]["mcp_tools:srv-1::search"] == "mcp__my_server__search"
    assert out["evidence"]["fs_write:/DATA/Documents/r"] == "write_file: /DATA/Documents/r/a.md"


def test_scan_drops_unresolvable_mcp_server_but_says_so():
    history = [_call("mcp__ghost__search", "{}")]
    out = draft.scan_history(history, mcp_id_by_slug={})
    assert out["preauth"]["mcp_tools"] == []
    assert "mcp__ghost__search" in out["evidence"]["dropped"]


def test_scan_records_skipped_compound_command():
    history = [_call("run_command", '{"command": "a && b"}')]
    out = draft.scan_history(history, mcp_id_by_slug={})
    assert out["preauth"]["shell"] == []
    assert "a && b" in out["evidence"]["dropped"]


def test_scan_survives_malformed_arguments():
    history = [
        _call("run_command", "not json"),
        _call("run_command", '{"command": 123}'),
        _call("run_command", '{"no_command": "x"}'),
        _call("write_file", '{"path": ""}'),
        {"type": "function_call"},           # 无 name / arguments
        "not a dict",
    ]
    out = draft.scan_history(history, mcp_id_by_slug={})
    assert out["preauth"]["shell"] == []
    assert out["preauth"]["fs_write"] == []


def test_scan_dedupes_and_bounds():
    history = [_call("run_command", '{"command": "date"}')] * 5
    out = draft.scan_history(history, mcp_id_by_slug={})
    assert out["preauth"]["shell"] == [{"kind": "prefix", "value": "date"}]

    import json as _json
    many = [_call("run_command", _json.dumps({"command": "cmd%d arg" % i}))
            for i in range(100)]
    out = draft.scan_history(many, mcp_id_by_slug={})
    assert len(out["preauth"]["shell"]) <= draft.MAX_RULES_PER_BUCKET


def test_scan_survives_unserializable_dict_arguments():
    history = [{"type": "function_call", "name": "other_tool",
                "arguments": {"when": {1, 2, 3}}}]
    out = draft.scan_history(history, mcp_id_by_slug={})
    assert out["preauth"]["shell"] == []
    assert out["suggested_egress"] == []


def test_scan_covers_all_write_tools():
    history = [
        _call("edit_file", '{"path": "/DATA/a/x.md"}'),
        _call("mkdir", '{"path": "/DATA/b/new"}'),
        _call("rename", '{"path": "/DATA/c/x", "dst": "/DATA/d/y"}'),
        _call("batch_fs", '{"operations": [{"op": "mkdir", "path": "/DATA/e/f"}]}'),
    ]
    out = draft.scan_history(history, mcp_id_by_slug={})
    assert out["preauth"]["fs_write"] == ["/DATA/a", "/DATA/b", "/DATA/c", "/DATA/d", "/DATA/e"]


# ---- 兜底与 LLM 输出解析 -------------------------------------------------

def test_fallbacks_use_user_messages_only():
    history = [
        {"role": "user", "content": "拉昨天的销售数据"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "写进飞书表格"},
    ]
    assert draft.fallback_prompt(history) == "拉昨天的销售数据\n\n写进飞书表格"
    assert draft.fallback_name(history) == "拉昨天的销售数据"


def test_fallback_name_truncates():
    history = [{"role": "user", "content": "x" * 100}]
    assert len(draft.fallback_name(history)) == draft.NAME_MAX_CHARS


def test_fallbacks_on_empty_history():
    assert draft.fallback_prompt([]) == ""
    assert draft.fallback_name([]) == ""


def test_parse_llm_draft():
    assert draft.parse_llm_draft('{"name": "n", "prompt": "p"}') == ("n", "p")
    # 模型爱加围栏
    assert draft.parse_llm_draft('```json\n{"name":"n","prompt":"p"}\n```') == ("n", "p")
    assert draft.parse_llm_draft("garbage") is None
    assert draft.parse_llm_draft('{"name": "n"}') is None
    assert draft.parse_llm_draft('{"name": "", "prompt": "p"}') is None
