"""Run arbitrary PowerShell. Always gated; full shell, no sanitization (spec §5)."""

import subprocess

from . import safety


def run_powershell(command: str) -> str:
    """Run a PowerShell command on the machine. GATED — stages, runs on confirm."""
    if not command or not command.strip():
        return "command must be a non-empty string."

    def _do() -> str:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=60,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip() or f"(exit {proc.returncode}, no output)"

    return safety.stage(f"run PowerShell: {command}", _do)
