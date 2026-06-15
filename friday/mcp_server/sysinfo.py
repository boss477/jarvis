"""System info: battery, CPU, RAM — read-only stats via psutil.

A dedicated tool so Friday answers "what's my battery?" reliably instead of
falling back to the flaky run_powershell escape-hatch. Output is one short,
voice-friendly line (low latency, small LLM context).
"""

import psutil


def system_info() -> str:
    """Report battery %, charging state, CPU load, and RAM usage."""
    parts = []

    batt = psutil.sensors_battery()
    if batt is None:
        parts.append("No battery (desktop or unknown)")
    else:
        state = "charging" if batt.power_plugged else "on battery"
        parts.append(f"Battery {int(batt.percent)}% ({state})")

    # interval=0.1: short blocking sample so the reading is real, not 0.0 (the
    # first non-blocking call always returns 0.0). Negligible against turn latency.
    parts.append(f"CPU {psutil.cpu_percent(interval=0.1):.0f}%")

    mem = psutil.virtual_memory()
    parts.append(f"RAM {mem.percent:.0f}% ({mem.used / 1e9:.1f} of {mem.total / 1e9:.1f} GB)")

    return ", ".join(parts) + "."
