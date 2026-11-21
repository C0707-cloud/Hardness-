"""Terminal log capture for obs-v1.

Two modes:
1. Tagged terminal log: parseable, produces structured events.
2. Raw terminal transcript: not parseable; imported as evidence gap + unknown.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from obs.parser import (
    ParseError,
    normalize_event,
    parse_lines,
    validate_event,
)


def parse_tagged_file(
    path: str | Path,
    run_id: str = "synthetic",
    session_id: str = "s1",
) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return parse_lines(lines, run_id=run_id, session_id=session_id, strict=True)


def import_raw_transcript(
    path: str | Path,
    run_id: str = "synthetic",
    session_id: str = "s1",
    ts: str | None = None,
) -> list[dict]:
    raw_path = Path(path)
    raw_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    raw_mtime = datetime.fromtimestamp(
        raw_path.stat().st_mtime
    ).astimezone().isoformat(timespec="seconds")
    timestamp = ts or raw_mtime
    base = {
        "schema_version": "obs-v1",
        "run_id": run_id,
        "session_id": session_id,
        "ts": timestamp,
        "source": "evaluator",
        "observability": "unknown",
        "actor": "evaluator",
        "evidence_grade": "E4",
        "raw_ref": str(raw_path),
        "raw_sha256": raw_sha256,
        "raw_mtime": raw_mtime,
    }
    gap = {
        **base,
        "event_id": f"EV-{run_id}-000001",
        "seq": 1,
        "event_type": "evidence.gap",
        "gap_type": "no_raw_structure",
        "affected_event_types": ["tool.call"],
        "impact": "unusable",
    }
    unknown = {
        **base,
        "event_id": f"EV-{run_id}-000002",
        "seq": 2,
        "event_type": "unknown.event",
        "unknown_kind": "unobservable",
        "reason": "no_raw_structure",
        "estimated_count": None,
        "affected_capabilities": [
            "H1.1", "H1.2", "H1.3", "H1.4",
            "H7.1", "H7.3", "H7.4",
            "H8.1", "H8.2", "H8.3",
        ],
    }
    for event in (gap, unknown):
        normalize_event(event)
        errors = validate_event(event)
        if errors:
            raise ParseError("; ".join(errors))
    return [gap, unknown]
