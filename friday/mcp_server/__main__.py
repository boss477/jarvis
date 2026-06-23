"""Friday tool server: one FastMCP instance, all tools registered flat, HTTP @ /mcp/."""

import os

from fastmcp import FastMCP

from . import apps, browser, calendar, chat, files, scheduler, shell, spotify, system, sysinfo, uia, windows
from . import input as input_tools
from . import safety

mcp = FastMCP("friday-tools")

_TOOLS = [
    apps.open_app, apps.open_url, apps.web_search, apps.close_app,
    system.set_volume, system.toggle_mute, system.set_brightness, system.media_control,
    system.lock_screen, system.sleep_pc, system.power_action,
    sysinfo.system_info, sysinfo.get_context,
    windows.get_active_window,
    files.open_path, files.search_files, files.create_folder, files.delete_path, files.move_path,
    shell.run_powershell,
    windows.list_windows, windows.focus_window, windows.minimize_window, windows.maximize_window,
    input_tools.type_text, input_tools.press_hotkey,
    uia.click_element, uia.list_elements,
    browser.browser_open, browser.browser_read, browser.browser_click,
    browser.browser_fill, browser.browser_close,
    chat.chatgpt_ask, chat.whatsapp_send, chat.comet_search, chat.research_topic,
    calendar.calendar_add,
    scheduler.schedule_task, scheduler.list_scheduled_tasks, scheduler.delete_scheduled_task,
    spotify.spotify_play,
    safety.confirm_action, safety.cancel_action,
]

for _fn in _TOOLS:
    mcp.add_tool(_fn)

EXPECTED_TOOL_NAMES = [fn.__name__ for fn in _TOOLS]

if __name__ == "__main__":
    # Loopback only — shell.py exposes a full shell. NEVER bind 0.0.0.0.
    # Port 8765 (not 8000) — 8000 was occupied by other local dev servers.
    # FRIDAY_MCP_PORT overrides it (tests use a separate port to avoid colliding
    # with a running instance).
    port = int(os.getenv("FRIDAY_MCP_PORT", "8765"))
    mcp.run(transport="http", host="127.0.0.1", port=port, path="/mcp/")
