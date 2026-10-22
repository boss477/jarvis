"""Optional Windows sign-in supervisor for the persistent agent host."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

from dotenv import load_dotenv

from .agent_host import AgentHostLock


def _state_path() -> Path:
    path = Path(os.getenv("AGENT_TASKS_PATH", "agent-state/tasks.jsonl"))
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


def _control_paths(state_path: Path) -> tuple[Path, Path]:
    return (Path(f"{state_path}.supervisor.lock"),
            Path(f"{state_path}.supervisor.stop"))


def request_stop(state_path: Path) -> bool:
    """Signal an active supervisor; return False when none currently owns it."""
    _, stop_path = _control_paths(state_path)
    try:
        probe = AgentHostLock(state_path, suffix=".supervisor.lock")
    except RuntimeError:
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text(str(time.time_ns()), encoding="ascii")
        return True
    else:
        probe.close()
        stop_path.unlink(missing_ok=True)
        # Retain the lock file; deleting an unlocked file can split lock identity
        # if another process opens it concurrently.
        return False


def _log_path() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) / "Friday" / "logs" if local_app_data else Path("logs")
    base.mkdir(parents=True, exist_ok=True)
    return base / "agent-supervisor.log"


def _append_log(log_path: Path, text: str) -> None:
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def run_supervisor(state_path: Path | None = None) -> None:
    state_path = state_path or _state_path()
    _, stop_path = _control_paths(state_path)
    log_path = _log_path()
    repository_root = Path(__file__).resolve().parents[2]
    start_time_ns = time.time_ns()

    try:
        lock = AgentHostLock(state_path, suffix=".supervisor.lock")
    except RuntimeError as exc:
        _append_log(log_path, f"Supervisor not started: {exc}")
        raise

    with lock:
        # A stop request left by a crashed/old supervisor must not stop a new one.
        # Preserve a request written after this supervisor began acquiring its lock.
        try:
            if int(stop_path.read_text(encoding="ascii").strip()) < start_time_ns:
                stop_path.unlink(missing_ok=True)
        except (FileNotFoundError, ValueError, OSError):
            pass

        delay = 2
        _append_log(log_path, "Supervisor started.")
        while not stop_path.exists():
            try:
                host_lock = AgentHostLock(state_path)
            except RuntimeError:
                _append_log(log_path, "Another agent host owns this task store; waiting before retry.")
                end = time.monotonic() + delay
                while time.monotonic() < end and not stop_path.exists():
                    time.sleep(min(0.5, max(0.0, end - time.monotonic())))
                delay = min(delay * 2, 60)
                continue
            else:
                host_lock.close()

            command = [sys.executable, "-m", "friday.services.agent_host"]
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = None
            try:
                with log_path.open("a", encoding="utf-8") as output:
                    process = subprocess.Popen(
                        command, cwd=repository_root, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT,
                        creationflags=creation_flags,
                    )
                    _append_log(log_path, f"Started agent host process {process.pid}.")
                    while process.poll() is None:
                        if stop_path.exists():
                            _append_log(log_path, "Stop requested; terminating agent host.")
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=5)
                            break
                        time.sleep(0.5)
                    exit_code = process.poll()
            except KeyboardInterrupt:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                _append_log(log_path, "Supervisor interrupted; agent host stopped.")
                raise

            if stop_path.exists():
                break
            _append_log(log_path, f"Agent host exited with code {exit_code}; restarting in {delay}s.")
            end = time.monotonic() + delay
            while time.monotonic() < end and not stop_path.exists():
                time.sleep(min(0.5, max(0.0, end - time.monotonic())))
            delay = min(delay * 2, 60)

        stop_path.unlink(missing_ok=True)
        _append_log(log_path, "Supervisor stopped.")


def main() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    load_dotenv(repository_root / "friday" / ".env")
    state_path = _state_path()
    if "--stop" in sys.argv[1:]:
        if request_stop(state_path):
            print("Stop requested for the Jarvis agent supervisor.")
        else:
            print("No Jarvis agent supervisor is currently running.")
        return
    run_supervisor(state_path)


if __name__ == "__main__":
    main()
