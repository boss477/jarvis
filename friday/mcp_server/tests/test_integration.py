import asyncio
import socket
import subprocess
import sys
import time

import pytest
from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient

URL = "http://127.0.0.1:8765/mcp/"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.3):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def server():
    proc = subprocess.Popen([sys.executable, "-m", "friday.mcp_server"])
    deadline = time.time() + 30
    while time.time() < deadline and not _port_open(8765):
        time.sleep(0.3)
    if not _port_open(8765):
        proc.terminate()
        pytest.fail("tool server did not start on port 8765")
    yield
    proc.terminate()
    proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_list_tools_over_http(server):
    client = MCPClient(StreamableHttpParameters(url=URL))
    await client.start()
    try:
        schema = await client.get_tools_schema()
        names = {t.name for t in schema.standard_tools}
        assert "cancel_action" in names
        assert len(schema.standard_tools) == 21
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_safe_tool_round_trip(server):
    # cancel_action has no side effects — safe to call end-to-end.
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    async with streamablehttp_client(url=URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("cancel_action", {})
            text = result.content[0].text
            assert "nothing to cancel" in text.lower()


@pytest.mark.asyncio
async def test_cold_server_start_raises_cleanly():
    # Nothing listening on 8099 -> start() must FAIL FAST, not hang.
    # wait_for guards against a silent hang; BaseException covers the anyio
    # CancelledError that surfaces from pipecat's connect teardown on a cold port.
    client = MCPClient(StreamableHttpParameters(url="http://127.0.0.1:8099/mcp/"))
    with pytest.raises(BaseException):
        await asyncio.wait_for(client.start(), timeout=15)
