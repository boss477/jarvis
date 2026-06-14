"""Friday tool server: one FastMCP instance, all tools registered flat, HTTP @ /mcp/."""

from fastmcp import FastMCP

from . import apps, files, shell, system
from . import input as input_tools
from . import safety

mcp = FastMCP("friday-tools")

_TOOLS = [
    apps.open_app, apps.open_url, apps.web_search, apps.close_app,
    system.set_volume, system.toggle_mute, system.set_brightness, system.media_control,
    system.lock_screen, system.sleep_pc, system.power_action,
    files.open_path, files.search_files, files.create_folder, files.delete_path, files.move_path,
    shell.run_powershell,
    input_tools.type_text, input_tools.press_hotkey,
    safety.confirm_action, safety.cancel_action,
]

for _fn in _TOOLS:
    mcp.add_tool(_fn)

EXPECTED_TOOL_NAMES = [fn.__name__ for fn in _TOOLS]

if __name__ == "__main__":
    # Loopback only — shell.py exposes a full shell. NEVER bind 0.0.0.0.
    # Port 8765 (not 8000) — 8000 was occupied by other local dev servers.
    mcp.run(transport="http", host="127.0.0.1", port=8765, path="/mcp/")
