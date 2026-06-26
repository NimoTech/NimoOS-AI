import json
import agent as agent_mod


def test_turn1_general_tool_schema_under_budget():
    """第 1 轮 general 的工具 schema 序列化体量要小(防止有人往常驻集乱加工具导致第 1 轮 prompt 反弹)。

    只统计第 1 轮模型实际会看到的工具(常驻 + expand_tools);门控工具虽在列表里
    但 is_enabled 为 False,不进 prompt。阈值按字符数粗算(~4 char/token)。
    """
    from skills import tool_gating as tg
    tg.UNLOCKED_VAR.set(set())          # 空解锁集 = 第 1 轮
    tools = agent_mod.select_tools_for_run([], session_id="s1", profile=None)
    visible = [t for t in tools
               if getattr(t, "is_enabled", True) is True
               or (callable(getattr(t, "is_enabled", True))
                   and t.is_enabled(None, None))]
    size = 0
    for t in visible:
        size += len(getattr(t, "name", ""))
        size += len(getattr(t, "description", "") or "")
        schema = getattr(t, "params_json_schema", None)
        if schema:
            size += len(json.dumps(schema, ensure_ascii=False))
    # 6 常驻 + expand_tools 的 schema 远小于此;留足余量,阈值约 ~2k token。
    assert size < 8000, f"第 1 轮工具 schema 体量过大: {size} 字符"
