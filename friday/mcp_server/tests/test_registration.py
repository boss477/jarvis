import pytest
from friday.mcp_server.__main__ import mcp, EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_all_expected_tools_registered():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(EXPECTED_TOOL_NAMES)
    assert len(EXPECTED_TOOL_NAMES) == 21
