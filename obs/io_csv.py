"""CSV read/write for obs-v1 events.

CSV is flat: nested values are JSON-encoded strings.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from obs.parser import normalize_event

FIELDS = [
    "schema_version",
    "event_id",
    "run_id",
    "session_id",
    "seq",
    "ts",
    "event_type",
    "source",
    "observability",
    "actor",
    "evidence_grade",
    "product",
    "product_version",
    "model_label",
    "model_fingerprint",
    "task_id",
    "workspace",
    "input_hash",
    "capture_source",
    "observability_tier",
    "authoritative_source",
    "trace_origin",
    "log_complete",
    "capabilities",
    "workspace_pre_hash",
    "end_ts",
    "wall_time_s",
    "terminate_reason",
    "completion_at_terminate",
    "evaluator_fallback",
    "timeout_source",
    "timeout_ts",
    "workspace_post_hash",
    "output_hashes",
    "phase",
    "call_id",
    "call_scope",
    "parent_call_id",
    "tool_name",
    "tool_kind",
    "args_digest",
    "args_summary",
    "validity",
    "invalid_kind",
    "unnecessary",
    "unnecessary_kind",
    "unnecessary_reason",
    "result_status",
    "exit_code",
    "output_digest",
    "output_ref",
    "stdout_digest",
    "stderr_digest",
    "duration_ms",
    "error_code",
    "error_message",
    "retryable",
    "reject_reason",
    "retry_of_call_id",
    "retry_scope",
    "retry_index",
    "retry_reason",
    "resolved_call_id",
    "retry_outcome",
    "recovered",
    "op",
    "path",
    "content_digest",
    "size",
    "encoding",
    "newline",
    "bom",
    "decision",
    "permission_id",
    "scope_type",
    "scope_value",
    "ui_action",
    "requested_by",
    "decided_by",
    "message",
    "evidence_ref",
    "kind",
    "permission_ref",
    "initial_prompt",
    "count",
    "turn_index",
    "unknown_kind",
    "reason",
    "estimated_count",
    "affected_capability",
    "gap_type",
    "affected_event_types",
    "affected_capabilities",
    "affected_event_ids",
    "affected_seqs",
    "impact",
    "raw_ref",
    "raw_sha256",
    "raw_mtime",
    "notes",
]

NESTED_FIELDS = {
    "capabilities",
    "args_summary",
    "output_hashes",
    "affected_event_types",
    "affected_capabilities",
    "affected_event_ids",
    "affected_seqs",
}


def _coerce(key: str, value: str):
    if value == "":
        return None
    if key in NESTED_FIELDS:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def read_csv(path: str | Path) -> list[dict]:
    events: list[dict] = []
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            event = {}
            for key, value in row.items():
                if key is None or value is None:
                    continue
                coerced = _coerce(key, value)
                if coerced is not None:
                    event[key] = coerced
            normalize_event(event)
            events.append(event)
    return events


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def write_csv(path: str | Path, events: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FIELDS,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for event in events:
            writer.writerow({key: _cell(event.get(key)) for key in FIELDS})
