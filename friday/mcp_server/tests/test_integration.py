import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest
from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient

# Use a dedicated test port so the suite never collides with a running
# Friday instance (run_friday.ps1 uses the default 8765).
TEST_PORT = 8766
URL = f"http://127.0.0.1:{TEST_PORT}/mcp/"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.3):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def server():
    env = {**os.environ, "FRIDAY_MCP_PORT": str(TEST_PORT)}
    proc = subprocess.Popen([sys.executable, "-m", "friday.mcp_server"], env=env)
    deadline = time.time() + 30
    while time.time() < deadline and not _port_open(TEST_PORT):
        time.sleep(0.3)
    if not _port_open(TEST_PORT):
        proc.terminate()
        pytest.fail(f"tool server did not start on port {TEST_PORT}")
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
        assert len(schema.standard_tools) == 36
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
