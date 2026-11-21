"""Upload-style chat capture adapter for obs-v1.

Input is a JSONL of chat records transcribed from the product UI:
  {"ts": "...", "role": "user", "text": "...", "attachment": "input.zip", "content_digest": "sha256:..."}
  {"ts": "...", "role": "assistant", "text": "...", "attachment": "output.zip", "content_digest": "sha256:..."}

The internal tool loop is not observable; the adapter records an
unknown.event and an evidence.gap instead of inventing tool calls.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from obs.parser import ParseError, normalize_event, validate_event

TOOL_CAPABILITIES = [
    "H1.1", "H1.2", "H1.3", "H1.4",
    "H7.1", "H7.3", "H7.4",
    "H8.1", "H8.2", "H8.3",
]


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _event(
    base: dict,
    seq: int,
    ts: str,
    event_type: str,
    source: str,
    observability: str,
    actor: str,
    **fields,
) -> dict:
    event = {
        **base,
        "seq": seq,
        "ts": ts,
        "event_type": event_type,
        "source": source,
        "observability": observability,
        "actor": actor,
        **fields,
    }
    event = {
        key: value for key, value in event.items()
        if value is not None or key == "estimated_count"
    }
    normalize_event(event)
    errors = validate_event(event)
    if errors:
        raise ParseError(f"upload event {seq}: {'; '.join(errors)}")
    return event


def parse_upload_log(
    path: str | Path,
    run_id: str = "upload",
    session_id: str = "s1",
    product: str = "upload",
    task_id: str = "T1",
    evidence_grade: str = "E4",
    terminate_reason: str = "assertions_passed",
    evaluator_fallback: bool = True,
    completion_at_terminate: float | None = None,
) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ParseError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            records.append(record)

    timestamps = [record.get("ts") for record in records if record.get("ts")]
    start_ts = timestamps[0] if timestamps else datetime.now().astimezone().isoformat()
    end_ts = timestamps[-1] if timestamps else start_ts
    try:
        wall_time_s = max(
            0.0,
            (datetime.fromisoformat(end_ts) - datetime.fromisoformat(start_ts)).total_seconds(),
        )
    except ValueError:
        wall_time_s = 0.0

    base = {
        "schema_version": "obs-v1",
        "run_id": run_id,
        "session_id": session_id,
        "evidence_grade": evidence_grade,
    }

    events = [
        _event(
            base, 1, start_ts, "run.start", "evaluator", "partial", "evaluator",
            event_id=f"EV-{run_id}-000001",
            product=product,
            task_id=task_id,
            log_complete=False,
            capabilities={},
            capture_source="upload_chat",
            observability_tier="unknown",
            trace_origin="evaluator_reconstructed",
        )
    ]

    seq = 2
    first_user_message = True
    for record in records:
        ts = record.get("ts") or start_ts
        role = record.get("role", "")
        text = record.get("text") or ""
        is_initial = False
        if role == "user":
            explicit = record.get("initial_prompt")
            is_initial = (
                bool(explicit) if explicit is not None else first_user_message
            )
            first_user_message = False
        if role == "user" and text:
            events.append(
                _event(
                    base, seq, ts, "user.input", "evaluator", "partial", "user",
                    event_id=f"EV-{run_id}-{seq:06d}",
                    kind="control" if is_initial else "semantic",
                    initial_prompt=is_initial,
                    content_digest=_sha256(text),
                    count=1,
                )
            )
            seq += 1
        attachment = record.get("attachment")
        if attachment:
            op = "upload" if role == "user" else "download"
            actor = "user" if role == "user" else "system"
            digest = record.get("content_digest")
            if op == "upload" and not digest:
                raise ParseError(f"upload missing content_digest at seq {seq}")
            events.append(
                _event(
                    base, seq, ts, "artifact.event", "evaluator", "partial", actor,
                    event_id=f"EV-{run_id}-{seq:06d}",
                    op=op,
                    path=str(attachment),
                    content_digest=digest,
                )
            )
            seq += 1

    events.append(
        _event(
            {**base, "evidence_grade": "E4"},
            seq, end_ts, "unknown.event", "evaluator", "unknown", "evaluator",
            event_id=f"EV-{run_id}-{seq:06d}",
            unknown_kind="unobservable",
            reason="no_tool_log",
            estimated_count=None,
            affected_capabilities=TOOL_CAPABILITIES,
        )
    )
    seq += 1

    events.append(
        _event(
            base, seq, end_ts, "run.end", "evaluator", "partial", "evaluator",
            event_id=f"EV-{run_id}-{seq:06d}",
            terminate_reason=terminate_reason,
            evaluator_fallback=evaluator_fallback,
            completion_at_terminate=completion_at_terminate,
            wall_time_s=wall_time_s,
        )
    )
    return events
