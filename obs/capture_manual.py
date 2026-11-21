"""Manual event entry via CSV."""

from __future__ import annotations

from pathlib import Path

from obs.io_csv import read_csv
from obs.parser import DEFAULTS, ParseError, normalize_event, validate_events


def parse_manual_csv(
    path: str | Path,
    run_id: str = "manual",
    session_id: str = "s1",
    source: str | None = None,
    evidence_grade: str | None = None,
    observability: str | None = None,
    capture_source: str | None = None,
) -> list[dict]:
    events = read_csv(path)
    for index, event in enumerate(events, 1):
        if source and "source" not in event:
            event["source"] = source
        if "evidence_grade" not in event:
            event["evidence_grade"] = evidence_grade or "E4"
        if observability and "observability" not in event:
            event["observability"] = observability
        for key, value in DEFAULTS.items():
            event.setdefault(key, value)
        if event.get("event_type") == "run.start":
            if not event.get("capture_source"):
                event["capture_source"] = capture_source or source or "manual"
            event.setdefault("trace_origin", "evaluator_reconstructed")
        event.setdefault("run_id", run_id)
        event.setdefault("session_id", session_id)
        event.setdefault("seq", index)
        event.setdefault("event_id", f"EV-{event['run_id']}-{event['seq']:06d}")
        normalize_event(event)
    report = validate_events(events)
    if report["errors"]:
        raise ParseError("; ".join(report["errors"]))
    return events
