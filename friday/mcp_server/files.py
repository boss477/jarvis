"""File operations. Destructive ops are sandboxed to ALLOWED_ROOTS and gated."""

import os
import shutil
from pathlib import Path

from . import safety

# Destructive ops may only touch paths inside these roots. Default: user home.
ALLOWED_ROOTS = [Path.home().resolve()]


def _safe_path(raw: str) -> Path:
    """Resolve raw to a real absolute path (following symlinks) and require it to
    be inside an allowed root. Raises ValueError otherwise."""
    p = Path(raw).expanduser().resolve()
    for root in ALLOWED_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise ValueError(f"path outside allowed roots: {p}")


def open_path(path: str) -> str:
    """Open a file or folder in Explorer / its default app (not sandboxed; read-only effect)."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"Path does not exist: {p}"
    os.startfile(str(p))
    return f"Opened {p}."


def search_files(query: str, directory: str | None = None) -> str:
    """Find files whose name contains query, under directory (default: home)."""
    base = Path(directory).expanduser() if directory else Path.home()
    if not base.exists():
        return f"Directory does not exist: {base}"
    matches = []
    for p in base.rglob(f"*{query}*"):
        matches.append(str(p))
        if len(matches) >= 50:
            break
    return "\n".join(matches) if matches else "No matches."


def create_folder(path: str) -> str:
    """Create a directory (within allowed roots)."""
    try:
        p = _safe_path(path)
    except ValueError as e:
        return str(e)
    p.mkdir(parents=True, exist_ok=True)
    return f"Created {p}."


def delete_path(path: str) -> str:
    """Delete a file or folder (within allowed roots). GATED."""
    try:
        p = _safe_path(path)
    except ValueError as e:
        return str(e)
    if not p.exists():
        return f"Path does not exist: {p}"

    def _do() -> str:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"Deleted {p}."

    return safety.stage(f"delete {p}", _do)


def move_path(src: str, dst: str) -> str:
    """Move a file or folder (both within allowed roots). GATED."""
    try:
        s = _safe_path(src)
        d = _safe_path(dst)
    except ValueError as e:
        return str(e)
    if not s.exists():
        return f"Source does not exist: {s}"

    def _do() -> str:
        shutil.move(str(s), str(d))
        return f"Moved {s} -> {d}."

    return safety.stage(f"move {s} to {d}", _do)
