"""JSONL read/write for obs-v1 events."""

from __future__ import annotations

import json
from pathlib import Path

from obs.parser import ParseError, normalize_event


def read_jsonl(path: str | Path) -> list[dict]:
    events: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ParseError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(event, dict):
                raise ParseError(f"{path}:{line_no}: event must be an object")
            normalize_event(event)
            events.append(event)
    return events


def write_jsonl(path: str | Path, events: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
