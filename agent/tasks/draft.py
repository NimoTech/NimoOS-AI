"""M6:把一段会话扫成定时任务草稿 —— 纯函数,无 I/O。

这里产出的每一条都是**建议**,最终授权由用户在认证 UI 里逐条确认(spec
§13)。因此本模块的偏向始终一致:宁可少建议,不可多授权。

两处刻意保守:
* shell 前缀取到子命令层级 —— 前缀越短权限越大,`lark-cli` 比
  `lark-cli base record` 危险得多;
* 出站域名根本不进 preauth —— 出站真相在 egress-proxy 的 TOFU 记录里,
  从命令文本抽 host 只是推测(命令里是变量就会漏,URL 出现在文档正文
  就会多),所以它走平行字段 `suggested_egress`。
"""
from __future__ import annotations

import json
import re
import shlex

# 前缀最多取几个 token。3 是"命令 + 子命令 + 子子命令"(`lark-cli base
# record`)的常见深度;再长就开始把参数当命令了。
MAX_PREFIX_TOKENS = 3

# 出现在 token 里就说明它是值不是子命令:路径、URL、host:port、赋值。
_VALUE_CHARS = ("/", ":", "=")

# 复合命令一律跳过:前缀规则匹配的是单条简单命令,`_run_allowlist_match`
# 也只放行单条简单命令,给 `a && b` 生成前缀只会产生一条永不命中的规则,
# 或者更糟 —— 让作者以为自己授权了实际没授权的东西。
_COMPOUND_RE = re.compile(r"[|;&><`\n]|\$\(")

_URL_RE = re.compile(r"https?://([^\s/'\"<>|]+)", re.IGNORECASE)

# 一个可用的主机名:方括号 IPv6 字面量,或普通域名/主机(字母数字开头结尾)。
# 抽出来的东西过不了这一关就丢掉 —— 推测项宁缺毋滥。
_HOST_RE = re.compile(r"^(?:\[[0-9a-f:]+\]|[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)$")

# 任务名硬截断,与 title_gen.TITLE_MAX_CHARS 同值(两处独立,不要互相 import
# —— title_gen 是会话标题,这里是任务名,将来可以各自调整)。
NAME_MAX_CHARS = 30

# 一个桶最多建议多少条。会话可能有几百次调用,建议列表长到用户不会逐条看
# 就失去了"逐条确认"的意义。
MAX_RULES_PER_BUCKET = 20

# 写类工具 → 参数里哪些键是路径。read_file / list_dir 等只读工具不在此列:
# fs_write 是写授权,读不需要它。
_WRITE_TOOL_PATH_KEYS = {
    "write_file": ("path",),
    "edit_file": ("path",),
    "delete_path": ("path",),
    "mkdir": ("path",),
    "rename": ("path", "dst"),
}

_MCP_PREFIX = "mcp__"

DRAFT_SYSTEM_PROMPT = (
    "You turn a chat transcript into ONE reusable instruction for an "
    "unattended scheduled agent run.\n"
    "The transcript is an iterative conversation; the instruction must be "
    "self-contained: keep concrete identifiers (URLs, table tokens, paths, "
    "recipients) that appeared, drop the back-and-forth.\n"
    "Write the instruction in the same language the user wrote in.\n"
    'Answer with JSON only: {"name": "<=16 chars", "prompt": "the instruction"}'
)


def normalize_prefix(command) -> str | None:
    """把一条命令归一成保守的授权前缀,拿不准就返回 None(跳过)。

    规则(spec §13.4):词法切分后自左取 token,首 token 必留;从第二个起
    遇到 ① 以 `-` 开头 ② 含 `/ : =` ③ 已取满 3 个,即停。
    """
    if not isinstance(command, str):
        return None
    text = command.strip()
    if not text or _COMPOUND_RE.search(text):
        return None
    try:
        tokens = shlex.split(text)
    except ValueError:
        # 引号不配对 —— 无法可靠判断哪个 token 是命令,不猜。
        return None
    if not tokens:
        return None

    taken = [tokens[0]]
    for tok in tokens[1:]:
        if len(taken) >= MAX_PREFIX_TOKENS:
            break
        if tok.startswith("-") or any(ch in tok for ch in _VALUE_CHARS):
            break
        taken.append(tok)

    # shlex 去掉了引号并按单空格重组,拼出来的前缀未必还是原命令的字面前缀:
    # `lark-cli base "record two" create` 会拼成 `lark-cli base record two`,
    # 它既匹配不回原命令,又能匹配一条从未跑过的
    # `lark-cli base record twofold-delete-everything`(preauth.shell_match 是
    # 纯 startswith)。授权范围只能是观察到的那条命令的字面前缀,所以逐个回退
    # 到不变式成立;退到无可退就放弃这条建议。
    while taken:
        prefix = " ".join(taken)
        if text.startswith(prefix):
            return prefix
        taken.pop()
    return None


def parse_mcp_call(name) -> tuple[str, str] | None:
    """`mcp__<slug>__<tool>` → (slug, tool);不是 MCP 调用则 None。"""
    if not isinstance(name, str) or not name.startswith(_MCP_PREFIX):
        return None
    rest = name[len(_MCP_PREFIX):]
    slug, sep, tool = rest.partition("__")
    if not sep or not slug or not tool:
        return None
    return slug, tool


def extract_hosts(text) -> list[str]:
    """从任意文本里抽 http(s) host,剥端口与尾随标点,去重保序。"""
    if not isinstance(text, str):
        return []
    out, seen = [], set()
    for raw in _URL_RE.findall(text):
        host = raw.split("@")[-1]          # 剥掉 user:pass@
        # URL 常出现在句子中间:`(见 https://a.com)`、`https://a.com.` ——
        # 尾随标点不是主机名的一部分。`]` 不在剥离集里,它是 IPv6 字面量的收尾。
        host = host.strip().rstrip(".,;)}>'\"").lower()
        # 剥端口;带方括号的 IPv6 字面量除外,它的 ':' 是地址的一部分。
        if ":" in host and not host.endswith("]"):
            host = host.rsplit(":", 1)[0]
        if not host or not _HOST_RE.match(host):
            continue
        if host not in seen:
            seen.add(host)
            out.append(host)
    return out


def _args_of(item: dict) -> dict:
    raw = item.get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parent_dir(path) -> str | None:
    if not isinstance(path, str):
        return None
    p = path.strip()
    if not p.startswith("/"):
        # 相对路径会按 run 的 CWD 解析 —— 永远不是作者的本意,不建议。
        return None
    parent = p.rsplit("/", 1)[0]
    return parent or "/"


def _batch_fs_paths(args: dict) -> list[str]:
    ops = args.get("operations")
    if not isinstance(ops, list):
        return []
    out = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        for key in ("path", "dst"):
            val = op.get(key)
            if isinstance(val, str) and val:
                out.append(val)
    return out


def _append(bucket: list, value, limit: int = MAX_RULES_PER_BUCKET) -> bool:
    """去重 + 限量追加。返回是否真的加进去了(供 evidence 判断)。"""
    if value in bucket or len(bucket) >= limit:
        return False
    bucket.append(value)
    return True


def scan_history(history, *, mcp_id_by_slug: dict) -> dict:
    """扫 history 里的 function_call,产出 preauth 建议 + 推测项 + 证据。

    `mcp_id_by_slug` 把 `mcp__<slug>__<tool>` 里的 slug 映回 MCP server id
    —— preauth 的契约是 `<server_id>::<tool>`(见 `_ensure_confirmed`),
    而 history 里存的是给模型看的 slug 名。映不回去的一律丢弃并记进
    evidence["dropped"],绝不猜一个 id。
    """
    shell: list[dict] = []
    mcp_tools: list[str] = []
    fs_write: list[str] = []
    suggested_egress: list[str] = []
    evidence: dict = {"dropped": []}

    for item in history or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = _args_of(item)

        mcp = parse_mcp_call(name)
        if mcp:
            slug, tool = mcp
            server_id = mcp_id_by_slug.get(slug)
            if not server_id:
                if name not in evidence["dropped"]:
                    evidence["dropped"].append(name)
                continue
            entry = f"{server_id}::{tool}"
            if _append(mcp_tools, entry):
                evidence[f"mcp_tools:{entry}"] = name
            continue

        if name == "run_command":
            command = args.get("command")
            prefix = normalize_prefix(command)
            if prefix is None:
                if isinstance(command, str) and command and command not in evidence["dropped"]:
                    evidence["dropped"].append(command)
                continue
            rule = {"kind": "prefix", "value": prefix}
            if _append(shell, rule):
                evidence[f"shell:{prefix}"] = command
            for host in extract_hosts(command):
                _append(suggested_egress, host)
            continue

        if name in _WRITE_TOOL_PATH_KEYS or name == "batch_fs":
            paths = (_batch_fs_paths(args) if name == "batch_fs"
                     else [args.get(k) for k in _WRITE_TOOL_PATH_KEYS[name]])
            for raw in paths:
                parent = _parent_dir(raw)
                if parent and _append(fs_write, parent):
                    evidence[f"fs_write:{parent}"] = f"{name}: {raw}"
            continue

        # 其余工具的参数里也可能出现 URL,只作推测,不影响 preauth。
        try:
            blob = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            # arguments 可能是已在内存里的 dict,含不可序列化的值;一条推测
            # 域名不值得让整趟扫描崩掉。
            continue
        for host in extract_hosts(blob):
            _append(suggested_egress, host)

    if not evidence["dropped"]:
        evidence.pop("dropped")

    return {
        "preauth": {
            "shell": shell,
            # 恒空:推测项不进授权文档(spec §13.4)。
            "egress_domains": [],
            "mcp_tools": mcp_tools,
            "fs_write": fs_write,
        },
        "suggested_egress": suggested_egress,
        "evidence": evidence,
    }


def _user_texts(history) -> list[str]:
    out = []
    for item in history or []:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") in
                     ("input_text", "text", "output_text")]
            text = "".join(parts).strip()
        else:
            text = ""
        if text:
            out.append(text)
    return out


def fallback_prompt(history) -> str:
    """模型不可用时的兜底:按顺序拼接用户原文。用户自己改写成可复跑指令。"""
    return "\n\n".join(_user_texts(history))


def fallback_name(history) -> str:
    texts = _user_texts(history)
    return texts[0][:NAME_MAX_CHARS] if texts else ""


def parse_llm_draft(raw) -> tuple[str, str] | None:
    """解析模型返回的 {"name","prompt"};任何不合格都返回 None(调用方兜底)。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        # 去掉 ```json ... ``` 围栏
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        obj = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    prompt = obj.get("prompt")
    if not isinstance(name, str) or not isinstance(prompt, str):
        return None
    name, prompt = name.strip(), prompt.strip()
    if not name or not prompt:
        return None
    return name[:NAME_MAX_CHARS], prompt
