import pytest
from friday.mcp_server.__main__ import mcp, EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_all_expected_tools_registered():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(EXPECTED_TOOL_NAMES)
    assert len(EXPECTED_TOOL_NAMES) == 36
    assert {"list_windows", "focus_window", "minimize_window", "maximize_window"} <= set(EXPECTED_TOOL_NAMES)
    assert "system_info" in EXPECTED_TOOL_NAMES
    assert {"click_element", "list_elements"} <= set(EXPECTED_TOOL_NAMES)
    assert {"browser_open", "browser_read", "browser_click", "browser_fill", "browser_close"} <= set(EXPECTED_TOOL_NAMES)
    assert {"schedule_task", "list_scheduled_tasks", "delete_scheduled_task"} <= set(EXPECTED_TOOL_NAMES)
