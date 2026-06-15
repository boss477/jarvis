"""Scheduling via Windows Task Scheduler (schtasks.exe) — the native API, no GUI.

Friday's tasks are namespaced with a `Friday_` prefix so list/delete can never
touch real system tasks. Creating a task runs an arbitrary command later
(deferred + persistent), so it's GATED like run_powershell; listing is read-only.
"""

import subprocess

from . import safety

PREFIX = "Friday_"


def _full_name(name: str) -> str:
    n = name.strip().replace(" ", "_")
    return n if n.startswith(PREFIX) else PREFIX + n


def schedule_task(name: str, command: str, time_hhmm: str) -> str:
    """Schedule `command` to run once at `time_hhmm` (24h HH:MM today). GATED."""
    if not name.strip() or not command.strip():
        return "name and command are required."
    parts = time_hhmm.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return "time_hhmm must look like '14:30'."
    task = _full_name(name)

    def _do() -> str:
        proc = subprocess.run(
            ["schtasks", "/create", "/tn", task, "/tr", command,
             "/sc", "ONCE", "/st", time_hhmm.strip(), "/f"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return f"Scheduled '{task}' for {time_hhmm}."
        return f"Could not schedule: {(proc.stderr or proc.stdout).strip()}"

    return safety.stage(f"schedule task '{task}' to run '{command}' at {time_hhmm}", _do)


def list_scheduled_tasks() -> str:
    """List Friday's scheduled tasks."""
    proc = subprocess.run(
        ["schtasks", "/query", "/fo", "LIST"], capture_output=True, text=True
    )
    names = [
        line.split(":", 1)[1].strip().lstrip("\\")
        for line in proc.stdout.splitlines()
        if line.startswith("TaskName:") and PREFIX in line
    ]
    return "\n".join(names) if names else "No Friday tasks scheduled."


def delete_scheduled_task(name: str) -> str:
    """Delete one of Friday's scheduled tasks by name."""
    task = _full_name(name)
    proc = subprocess.run(
        ["schtasks", "/delete", "/tn", task, "/f"], capture_output=True, text=True
    )
    if proc.returncode == 0:
        return f"Deleted '{task}'."
    return f"Could not delete '{task}': {(proc.stderr or proc.stdout).strip()}"
