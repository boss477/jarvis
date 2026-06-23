"""Concurrency-safety declarations for Jarvis tools.

A tool is concurrency-safe if it only reads state and has no ordering
dependencies with other tools. Conservative by default: only pure reads
are listed here. Everything else serialises.
"""

CONCURRENCY_SAFE_TOOLS: frozenset[str] = frozenset({
    # Pure system reads
    "system_info",
    "get_context",
    "get_active_window",
    "list_windows",
    # Filesystem reads
    "search_files",
    # UI reads
    "list_elements",
    # Browser reads
    "browser_read",
    # Scheduler reads
    "list_scheduled_tasks",
})
