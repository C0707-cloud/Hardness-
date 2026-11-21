"""Desktop UI capture adapter for obs-v1.

Input is a JSONL of UI actions (for example, transcribed from a screen
recording). The adapter translates actions into obs-v1 events; it does not
scrape the UI itself.

Action records:
  {"ts": "...", "action": "authorize", "target": "...", "result": "granted"}
  {"ts": "...", "action": "upload", "target": "...", "content_digest": "sha256:..."}
  {"ts": "...", "action": "message", "kind": "semantic", "text": "...", "initial_prompt": false}
  {"ts": "...", "action": "tool_call", "phase": "completed", "tool_name": "read_file",
   "args_digest": "sha256:...", "result_status": "success"}
  {"ts": "...", "action": "tool_invisible"}
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from obs.parser import PHASE_RESULT, ParseError, normalize_event, validate_event

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
        raise ParseError(f"desktop event {seq}: {'; '.join(errors)}")
    return event


def parse_desktop_log(
    path: str | Path,
    run_id: str = "desktop",
    session_id: str = "s1",
    product: str = "desktop",
    task_id: str = "T1",
    evidence_grade: str = "E4",
    terminate_reason: str = "assertions_passed",
    evaluator_fallback: bool = False,
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
            base, 1, start_ts, "run.start", "ui_capture", "partial", "evaluator",
            event_id=f"EV-{run_id}-000001",
            product=product,
            task_id=task_id,
            log_complete=False,
            capabilities={},
            capture_source="desktop_log",
            observability_tier="partial",
            trace_origin="evaluator_reconstructed",
        )
    ]

    seq = 2
    for record in records:
        ts = record.get("ts") or start_ts
        action = record.get("action", "")
        if action in ("authorize", "permission"):
            if not record.get("result"):
                raise ParseError(f"desktop authorize missing result at seq {seq}")
            events.append(
                _event(
                    base, seq, ts, "permission.event", "ui_capture", "partial", "user",
                    event_id=f"EV-{run_id}-{seq:06d}",
                    decision=record.get("result"),
                    scope_type="ui",
                    scope_value=record.get("target") or "unknown",
                    ui_action=record.get("ui_action", action),
                    permission_id=record.get("permission_id"),
                    requested_by="agent",
                    decided_by="user",
                    message=record.get("note"),
                )
            )
        elif action in ("upload", "download"):
            digest = record.get("content_digest")
            if action == "upload" and not digest:
                raise ParseError(f"desktop upload missing content_digest at seq {seq}")
            events.append(
                _event(
                    base, seq, ts, "artifact.event", "ui_capture", "partial", "user",
                    event_id=f"EV-{run_id}-{seq:06d}",
                    op=action,
                    path=record.get("target") or "unknown",
                    content_digest=digest,
                )
            )
        elif action in ("message", "input"):
            if not record.get("kind"):
                raise ParseError(f"desktop message missing kind at seq {seq}")
            text = record.get("text") or record.get("note") or ""
            if not text:
                raise ParseError(f"desktop message missing text at seq {seq}")
            events.append(
                _event(
                    base, seq, ts, "user.input", "ui_capture", "partial", "user",
                    event_id=f"EV-{run_id}-{seq:06d}",
                    kind=record.get("kind"),
                    initial_prompt=record.get("initial_prompt"),
                    content_digest=_sha256(text),
                    count=1,
                )
            )
        elif action == "tool_call":
            phase = record.get("phase")
            if phase not in PHASE_RESULT:
                raise ParseError(
                    f"desktop tool_call missing/invalid phase at seq {seq}"
                )
            tool_name = record.get("tool_name")
            if not tool_name:
                raise ParseError(f"desktop tool_call missing tool_name at seq {seq}")
            call_id = record.get("call_id")
            if not call_id:
                raise ParseError(f"desktop tool_call missing call_id at seq {seq}")
            args_digest = record.get("args_digest")
            args_summary = record.get("args_summary")
            if not args_digest and args_summary is None:
                raise ParseError(f"desktop tool_call missing args at seq {seq}")
            events.append(
                _event(
                    base, seq, ts, "tool.call", "ui_capture", "partial", "agent",
                    event_id=f"EV-{run_id}-{seq:06d}",
                    phase=phase,
                    call_id=call_id,
                    call_scope=record.get("call_scope", "top_level"),
                    tool_name=tool_name,
                    tool_kind=record.get("tool_kind"),
                    args_digest=args_digest,
                    args_summary=args_summary,
                    result_status=record.get("result_status") or PHASE_RESULT[phase],
                    exit_code=record.get("exit_code"),
                    output_digest=record.get("output_digest"),
                    output_ref=record.get("output_ref"),
                    stdout_digest=record.get("stdout_digest"),
                    stderr_digest=record.get("stderr_digest"),
                    error_code=record.get("error_code"),
                    error_message=record.get("error_message"),
                    reject_reason=record.get("reject_reason"),
                    retryable=record.get("retryable"),
                    duration_ms=record.get("duration_ms"),
                )
            )
        elif action in ("tool_invisible", "unknown"):
            events.append(
                _event(
                    {**base, "evidence_grade": "E4"},
                    seq, ts, "unknown.event", "ui_capture", "unknown", "evaluator",
                    event_id=f"EV-{run_id}-{seq:06d}",
                    unknown_kind="unobservable",
                    reason="no_tool_log",
                    estimated_count=None,
                    affected_capabilities=TOOL_CAPABILITIES,
                )
            )
        else:
            raise ParseError(f"unknown desktop action: {action}")
        seq += 1

    events.append(
        _event(
            base, seq, end_ts, "run.end", "ui_capture", "partial", "evaluator",
            event_id=f"EV-{run_id}-{seq:06d}",
            terminate_reason=terminate_reason,
            evaluator_fallback=evaluator_fallback,
            completion_at_terminate=completion_at_terminate,
            wall_time_s=wall_time_s,
        )
    )
    return events
