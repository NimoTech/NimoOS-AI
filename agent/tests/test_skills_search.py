import pytest

from skills import ALL_TOOLS


def test_search_tools_in_all_tools():
    tool_names = {t.name for t in ALL_TOOLS}
    assert "nimoos_search" in tool_names
    assert "read_file_chunk" in tool_names
