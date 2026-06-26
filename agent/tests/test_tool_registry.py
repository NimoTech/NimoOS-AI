from skills import ALL_TOOLS
from skills import tool_registry as tr


def _name(t):
    return getattr(t, "name", getattr(t, "__name__", None))


def test_core_names_exact():
    assert tr.CORE_TOOL_NAMES == frozenset(
        {"run_command", "read_file", "list_dir",
         "nimoos_search", "list_skills", "read_skill_file"})


def test_categories_exact_keys():
    assert set(tr.CATEGORY_TOOLS.keys()) == {
        "apps", "files", "photos", "wiki",
        "documents", "system", "events", "memory", "mcp"}
    assert set(tr.CATEGORY_DESCRIPTIONS.keys()) == set(tr.CATEGORY_TOOLS.keys())


def test_partition_is_complete_and_disjoint():
    # 每个静态工具恰好属于 CORE 或某一类,无孤儿、无重复。
    all_names = [_name(t) for t in ALL_TOOLS]
    assert len(all_names) == len(set(all_names)), "ALL_TOOLS 有重名"
    categorized = []
    for tools in tr.CATEGORY_TOOLS.values():
        categorized += [_name(t) for t in tools]
    # mcp 类的静态成员只有 mcp_register_server(运行时 MCP 工具是动态附加,不在此)
    covered = set(tr.CORE_TOOL_NAMES) | set(categorized)
    assert covered == set(all_names), (
        f"未覆盖: {set(all_names) - covered}; 多余: {covered - set(all_names)}")
    assert len(categorized) == len(set(categorized)), "某工具被分进多个类别"


def test_category_of():
    assert tr.category_of("install_app") == "apps"
    assert tr.category_of("write_file") == "files"
    assert tr.category_of("run_command") is None      # 常驻
    assert tr.category_of("does_not_exist") is None
