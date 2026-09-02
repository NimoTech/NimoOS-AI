from skills import ALL_TOOLS
from skills import tool_registry as tr


def _name(t):
    return getattr(t, "name", getattr(t, "__name__", None))


def test_core_names_exact():
    assert tr.CORE_TOOL_NAMES == frozenset(
        {"run_command", "read_file", "list_dir",
         "nimoos_search", "read_document", "read_file_chunk", "read_skill_file",
         "remember", "forget", "recall"})


def test_document_readers_are_core_not_gated():
    # Regression: the retrieval path must not need expand_tools. Only the
    # vision-dependent page renderer stays behind the "documents" gate.
    assert tr.category_of("read_document") is None
    assert tr.category_of("read_file_chunk") is None
    assert [_name(t) for t in tr.CATEGORY_TOOLS["documents"]] == ["view_document_page"]


def test_categories_exact_keys():
    assert set(tr.CATEGORY_TOOLS.keys()) == {
        "apps", "files", "photos", "wiki",
        "documents", "system", "events", "mcp", "notes", "toolbox", "web",
        "tasks"}
    assert set(tr.CATEGORY_DESCRIPTIONS.keys()) == set(tr.CATEGORY_TOOLS.keys())


def test_partition_is_complete_and_disjoint():
    # every static tool belongs to exactly CORE or one category, no orphans, no duplicates.
    all_names = [_name(t) for t in ALL_TOOLS]
    assert len(all_names) == len(set(all_names)), "ALL_TOOLS has duplicate names"
    categorized = []
    for tools in tr.CATEGORY_TOOLS.values():
        categorized += [_name(t) for t in tools]
    # the mcp category's only static member is add_mcp_server (runtime MCP tools are attached dynamically, not here)
    covered = set(tr.CORE_TOOL_NAMES) | set(categorized)
    assert covered == set(all_names), (
        f"uncovered: {set(all_names) - covered}; extra: {covered - set(all_names)}")
    assert len(categorized) == len(set(categorized)), "a tool was placed in multiple categories"


def test_category_of():
    assert tr.category_of("install_app") == "apps"
    assert tr.category_of("write_file") == "files"
    assert tr.category_of("run_command") is None      # always-on
    assert tr.category_of("does_not_exist") is None
