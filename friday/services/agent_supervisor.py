"""Optional Windows sign-in supervisor for the persistent agent host."""
from __future__ import annotations

import codecs
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from dotenv import load_dotenv

from .agent_host import AgentHostLock
from .jsonl_store import _jsonl_file_lock

_MAX_LOG_BYTES = 1_048_576
_LOG_BACKUP_COUNT = 2
_MAX_LOG_MESSAGE_BYTES = 8192
_HOST_OUTPUT_READ_CHARS = 2048
_HOST_OUTPUT_RECORD_CHARS = 1800
_LOG_LOCK = threading.Lock()


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
        # Remove a stale marker while still holding the supervisor lock. If the
        # lock is released first, a new supervisor or concurrent stop request
        # can race with this unlink and lose a fresh stop request.
        try:
            stop_path.unlink(missing_ok=True)
        finally:
            probe.close()
        # Retain the lock file; deleting an unlocked file can split lock identity
        # if another process opens it concurrently.
        return False



def _agent_host_is_running(state_path: Path) -> bool:
    """Detect a host that outlived its supervisor by probing the host lock."""
    try:
        probe = AgentHostLock(state_path)
    except RuntimeError:
        return True
    else:
        probe.close()
        return False


def _log_path() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) / "Friday" / "logs" if local_app_data else Path("logs")
    base.mkdir(parents=True, exist_ok=True)
    return base / "agent-supervisor.log"


def _append_log(log_path: Path, text: str) -> None:
    """Append one bounded log record, rotating the active log and two backups."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    message = str(text)
    max_message_bytes = max(1, min(_MAX_LOG_MESSAGE_BYTES, _MAX_LOG_BYTES - 64))
    encoded_message = message.encode("utf-8", errors="replace")
    if len(encoded_message) > max_message_bytes:
        message = encoded_message[:max_message_bytes].decode("utf-8", errors="ignore")
        message += " [truncated]"
    record = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n".encode("utf-8")

    # The in-process lock protects threads; the JSONL sidecar lock also
    # serializes rotation and appends across independent supervisor processes.
    with _LOG_LOCK, _jsonl_file_lock(log_path):
        try:
            current_size = log_path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size + len(record) > _MAX_LOG_BYTES and current_size:
            try:
                for index in range(_LOG_BACKUP_COUNT, 1, -1):
                    source = Path(f"{log_path}.{index - 1}")
                    target = Path(f"{log_path}.{index}")
                    target.unlink(missing_ok=True)
                    if source.exists():
                        os.replace(source, target)
                first_backup = Path(f"{log_path}.1")
                first_backup.unlink(missing_ok=True)
                if log_path.exists():
                    os.replace(log_path, first_backup)
            except OSError:
                # Rotation is best-effort on Windows, where another process
                # may briefly hold a backup open. Keep the active log writable.
                pass
        # A failed rotation must not let an already-full active log grow.
        # If rotation succeeded, the active path is absent and starts at zero.
        try:
            active_size = log_path.stat().st_size
        except FileNotFoundError:
            active_size = 0
        if active_size + len(record) > _MAX_LOG_BYTES:
            return
        # Remove unexpected numeric backups too, so stray .3/.4 files cannot
        # defeat the configured total-size bound. Ignore non-numeric sidecars
        # such as the persistent .mutex lock file.
        for backup in log_path.parent.glob(f"{log_path.name}.*"):
            suffix = backup.name[len(log_path.name) + 1:]
            if suffix.isdigit() and int(suffix) > _LOG_BACKUP_COUNT:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass
        with log_path.open("ab") as handle:
            handle.write(record)

def _stream_host_output(stream, log_path: Path) -> None:
    """Drain child output even when log writes fail, discarding unrecordable output."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""

    def append_record(message: str) -> None:
        try:
            _append_log(log_path, message)
        except (OSError, ValueError):
            # Drop only this record. A transient lock or disk error must not
            # disable diagnostics for later records from the same child.
            return

    read_chunk = getattr(stream, "read1", None) or stream.read
    try:
        while True:
            chunk = read_chunk(_HOST_OUTPUT_READ_CHARS)
            if not chunk:
                break
            if isinstance(chunk, bytes):
                chunk = decoder.decode(chunk)
            pending += chunk
            while True:
                newline = pending.find("\n")
                if newline >= 0 and newline <= _HOST_OUTPUT_RECORD_CHARS:
                    line = pending[:newline].rstrip("\r")
                    pending = pending[newline + 1:]
                    append_record("host: " + line)
                    continue
                if len(pending) > _HOST_OUTPUT_RECORD_CHARS:
                    part = pending[:_HOST_OUTPUT_RECORD_CHARS]
                    pending = pending[_HOST_OUTPUT_RECORD_CHARS:]
                    append_record("host: " + part + " [continued]")
                    continue
                break
        # Flush any incomplete UTF-8 sequence at EOF with replacement, rather
        # than losing buffered bytes from the final chunk.
        pending += decoder.decode(b"", final=True)
        if pending:
            append_record("host: " + pending.rstrip("\r"))
    except (OSError, ValueError) as exc:
        append_record(f"Could not read agent host output ({type(exc).__name__}).")
    finally:
        try:
            stream.close()
        except OSError:
            pass

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

            command = [sys.executable, "-u", "-m", "friday.services.agent_host"]
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = None
            output_thread = None
            output_thread_started = False
            stop_requested = False
            try:
                child_env = os.environ.copy()
                child_env["AGENT_TASKS_PATH"] = str(state_path.resolve())
                process = subprocess.Popen(
                    command, cwd=repository_root, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=creation_flags, env=child_env,
                )
                output_stream = getattr(process, "stdout", None)
                if output_stream is not None:
                    output_thread = threading.Thread(
                        target=_stream_host_output, args=(output_stream, log_path),
                        name="agent-host-log", daemon=False,
                    )
                    output_thread.start()
                    output_thread_started = True
                _append_log(log_path, f"Started agent host process {process.pid}.")
                while process.poll() is None:
                    if stop_path.exists():
                        stop_requested = True
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
                stop_requested = True
                _append_log(log_path, "Supervisor interrupted; agent host stop requested.")
                raise
            finally:
                # Any exception after Popen must not leave an unsupervised host.
                if process is not None and process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if output_thread is not None and output_thread_started:
                    output_thread.join()
                elif process is not None:
                    output_stream = getattr(process, "stdout", None)
                    if output_stream is not None:
                        output_stream.close()
                if stop_requested and process is not None:
                    stopped_code = process.poll()
                    if stopped_code is None:
                        _append_log(log_path, "Agent host did not stop; process remains active.")
                    else:
                        _append_log(log_path, f"Agent host stopped with code {stopped_code}.")

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
            return
        if _agent_host_is_running(state_path):
            print(
                "No Jarvis supervisor is running, but an agent host still owns "
                "this task store; it may be orphaned and remains active."
            )
        else:
            print("No Jarvis agent supervisor is currently running; stop was not confirmed.")
        # The PowerShell remover treats a non-zero exit as an unconfirmed stop.
        # Never let it report success when no supervisor accepted the request.
        raise SystemExit(1)
    run_supervisor(state_path)


if __name__ == "__main__":
    main()
