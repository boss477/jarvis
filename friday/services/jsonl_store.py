"""Strict JSONL reading with recovery for a process-crashed final append."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


def read_jsonl_records(path: str | Path) -> Iterator[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        return
    lines = source.read_bytes().splitlines(keepends=True)
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        is_final_partial = index == len(lines) - 1 and not raw.endswith((b"\n", b"\r"))
        try:
            line = raw.decode("utf-8")
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if is_final_partial:
                return
            raise ValueError(f"invalid JSONL record at {source}:{index + 1}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"expected JSON object at {source}:{index + 1}")
        yield record


def append_jsonl_record(path: str | Path, record: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        data = target.read_bytes()
        if data and not data.endswith((b"\n", b"\r")):
            last_newline = max(data.rfind(b"\n"), data.rfind(b"\r"))
            offset = last_newline + 1
            tail = data[offset:]
            try:
                decoded = tail.decode("utf-8")
                parsed = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                # A malformed non-terminated final record is the interrupted
                # append. Keep all preceding committed records intact.
                with target.open("r+b") as handle:
                    handle.truncate(offset)
            else:
                if not isinstance(parsed, dict):
                    with target.open("r+b") as handle:
                        handle.truncate(offset)
                else:
                    with target.open("ab") as handle:
                        handle.write(b"\n")
    encoded = json.dumps(record, ensure_ascii=False, default=str).encode("utf-8") + b"\n"
    with target.open("ab") as handle:
        handle.write(encoded)
