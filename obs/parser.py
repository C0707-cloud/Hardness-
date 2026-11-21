"""obs-v1: event model, tagged parser, validation, and summary.

This module is deliberately scoring-neutral: it records facts and counts.
It does not evaluate products and does not assign H1-H8 scores.
"""

from __future__ import annotations

import json
import re
import hashlib
from collections import Counter
from datetime import datetime, timezone

EVENT_TYPES = {
    "run.start",
    "run.end",
    "tool.call",
    "tool.retry",
    "artifact.event",
    "permission.event",
    "user.input",
    "unknown.event",
    "evidence.gap",
}

PHASES = {"requested", "completed", "failed", "rejected"}
CALL_SCOPES = {"top_level", "subcall"}
RETRY_SCOPES = {"internal", "top_level"}
SOURCES = {"product_log", "terminal_capture", "ui_capture", "evaluator", "manual"}
OBSERVABILITY = {"visible", "partial", "unknown"}
ACTORS = {"agent", "user", "evaluator", "system"}
EVIDENCE_GRADES = {"E1", "E2", "E3", "E4"}
PERMISSION_DECISIONS = {"requested", "granted", "denied", "blocked", "allowed"}
SCOPE_TYPES = {"path", "network", "command", "ui", "upload"}
USER_INPUT_KINDS = {"semantic", "mechanical", "control"}
UNKNOWN_KINDS = {"unknown", "unobservable"}
GAP_TYPES = {
    "missing_timestamp",
    "missing_args",
    "missing_result",
    "no_raw_log",
    "no_raw_structure",
}
IMPACTS = {"partial", "unusable"}
ARTIFACT_OPS = {"create", "modify", "delete", "upload", "download"}
RUBRIC_SUBITEMS = {
    "H1.1", "H1.2", "H1.3", "H1.4",
    "H2.1", "H2.2", "H2.3",
    "H3.1", "H3.2", "H3.3",
    "H4.1", "H4.2", "H4.3", "H4.4",
    "H5.1", "H5.2", "H5.3",
    "H6.1", "H6.2", "H6.3",
    "H7.1", "H7.2", "H7.3", "H7.4",
    "H8.1", "H8.2", "H8.3",
}
VALIDITY = {"valid", "invalid", "unknown"}
INVALID_KINDS = {
    "unknown_tool",
    "schema_error",
    "missing_arg",
    "type_error",
    "bad_reference",
    "permission_denied",
    "other",
}
INVALID_COUNT_KINDS = INVALID_KINDS - {"permission_denied"}
TOOL_KINDS = {"read", "write", "command", "other", "unknown"}
UNNECESSARY_KINDS = {"duplicate", "irrelevant", "overkill", "other"}
TIMEOUT_SOURCES = {"product", "evaluator"}
RETRY_OUTCOMES = {"recovered", "failed", "unknown"}
TRACE_ORIGINS = {"product_exported", "evaluator_reconstructed", "mixed"}
H8_PRODUCT_LOG_THRESHOLD = 0.8
SOURCE_PRIORITY = {
    "product_log": 4,
    "terminal_capture": 3,
    "ui_capture": 3,
    "manual": 2,
    "evaluator": 1,
}

DEFAULTS = {
    "schema_version": "obs-v1",
    "session_id": "s1",
    "source": "manual",
    "observability": "visible",
    "actor": "agent",
    "evidence_grade": "E3",
}

PHASE_RESULT = {
    "requested": "pending",
    "completed": "success",
    "failed": "error",
    "rejected": "denied",
}


class ParseError(ValueError):
    pass


def _split_pairs(text: str) -> list[str]:
    """Split key=value pairs on ';' but ignore ';' inside JSON/quotes."""
    parts, buf = [], []
    depth = 0
    quote = None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "{[(":
            depth += 1
            buf.append(ch)
        elif ch in "}])":
            depth -= 1
            buf.append(ch)
        elif ch == ";" and depth == 0:
            if buf:
                parts.append("".join(buf).strip())
                buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _parse_value(text: str):
    t = text.strip()
    if t == "true":
        return True
    if t == "false":
        return False
    if t == "null":
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    if t.startswith(("{", "[")):
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            return t
    return t


def parse_line(line: str) -> dict:
    if "|" not in line:
        raise ParseError("line must be: <ts> | <event_type> | <key=value;...>")
    parts = [p.strip() for p in line.split("|", 2)]
    if len(parts) != 3:
        raise ParseError("line must have 3 parts separated by '|'")
    ts, event_type, body = parts
    if event_type not in EVENT_TYPES:
        raise ParseError(f"unknown event_type: {event_type}")
    fields = {"ts": ts, "event_type": event_type}
    for pair in _split_pairs(body):
        if "=" not in pair:
            raise ParseError(f"bad field: {pair}")
        key, value = pair.split("=", 1)
        fields[key.strip()] = _parse_value(value)
    return fields


def normalize_event(event: dict) -> None:
    """Fill defaults that are safe to infer from the event type."""
    et = event.get("event_type")
    if et == "run.start":
        event.setdefault("log_complete", False)
        event.setdefault("capabilities", {})
    elif et == "run.end":
        event.setdefault("evaluator_fallback", False)
    elif et == "tool.call":
        if "phase" in event:
            event.setdefault("call_scope", "top_level")
            event.setdefault("result_status", PHASE_RESULT.get(event["phase"]))
    elif et == "unknown.event":
        event.setdefault("unknown_kind", "unknown")
        event.setdefault("estimated_count", None)
        if "affected_capabilities" not in event and event.get("affected_capability"):
            event["affected_capabilities"] = [event["affected_capability"]]
    elif et == "user.input":
        event.setdefault("count", 1)
    elif et == "tool.retry":
        event.setdefault("retry_outcome", "unknown")
        if event.get("retry_outcome") == "recovered":
            event.setdefault("recovered", True)
        elif event.get("retry_outcome") == "failed":
            event.setdefault("recovered", False)
        if event.get("recovered") is True:
            event["retry_outcome"] = "recovered"
    elif et == "evidence.gap":
        event.setdefault("impact", "partial")


def validate_event(event: dict) -> list[str]:
    errors: list[str] = []
    et = event.get("event_type")
    if et not in EVENT_TYPES:
        return [f"unknown event_type: {et}"]

    for field in (
        "schema_version",
        "event_id",
        "run_id",
        "session_id",
        "ts",
        "source",
        "observability",
        "actor",
        "evidence_grade",
    ):
        if not event.get(field):
            errors.append(f"missing {field}")

    if event.get("schema_version") != "obs-v1":
        errors.append("schema_version must be obs-v1")
    if event.get("source") not in SOURCES:
        errors.append(f"bad source: {event.get('source')}")
    if event.get("observability") not in OBSERVABILITY:
        errors.append(f"bad observability: {event.get('observability')}")
    if event.get("actor") not in ACTORS:
        errors.append(f"bad actor: {event.get('actor')}")
    if event.get("evidence_grade") not in EVIDENCE_GRADES:
        errors.append(f"bad evidence_grade: {event.get('evidence_grade')}")

    seq = event.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        errors.append("seq must be a positive integer")

    try:
        datetime.fromisoformat(str(event.get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        errors.append("ts must be ISO 8601")

    if et == "run.start":
        for field in ("product", "task_id"):
            if not event.get(field):
                errors.append(f"run.start missing {field}")
        if not isinstance(event.get("log_complete"), bool):
            errors.append("run.start log_complete must be boolean")
        if not isinstance(event.get("capabilities"), dict):
            errors.append("run.start capabilities must be an object")
        if "workspace_pre_hash" in event and not isinstance(event.get("workspace_pre_hash"), str):
            errors.append("workspace_pre_hash must be a string")
        if "authoritative_source" in event and event.get("authoritative_source") not in SOURCES:
            errors.append(f"bad authoritative_source: {event.get('authoritative_source')}")
        if "trace_origin" in event and event.get("trace_origin") not in TRACE_ORIGINS:
            errors.append(f"bad trace_origin: {event.get('trace_origin')}")

    elif et == "run.end":
        if not event.get("terminate_reason"):
            errors.append("run.end missing terminate_reason")
        if not isinstance(event.get("evaluator_fallback"), bool):
            errors.append("run.end evaluator_fallback must be boolean")
        wall = event.get("wall_time_s")
        if isinstance(wall, bool) or not isinstance(wall, (int, float)):
            errors.append("run.end wall_time_s must be a number")
        completion = event.get("completion_at_terminate")
        if completion is not None:
            if (
                isinstance(completion, bool)
                or not isinstance(completion, (int, float))
                or not (0 <= completion <= 100)
            ):
                errors.append("completion_at_terminate must be a number between 0 and 100")
        if "timeout_source" in event and event.get("timeout_source") not in TIMEOUT_SOURCES:
            errors.append(f"bad timeout_source: {event.get('timeout_source')}")
        if event.get("timeout_ts"):
            try:
                datetime.fromisoformat(str(event["timeout_ts"]).replace("Z", "+00:00"))
            except ValueError:
                errors.append("timeout_ts must be ISO 8601")
        if "workspace_post_hash" in event and not isinstance(event.get("workspace_post_hash"), str):
            errors.append("workspace_post_hash must be a string")
        if "output_hashes" in event and not isinstance(event.get("output_hashes"), list):
            errors.append("output_hashes must be a list")

    elif et == "tool.call":
        for field in ("phase", "call_id", "call_scope", "tool_name"):
            if not event.get(field):
                errors.append(f"tool.call missing {field}")
        if event.get("phase") not in PHASES:
            errors.append(f"bad tool.call phase: {event.get('phase')}")
        if event.get("call_scope") not in CALL_SCOPES:
            errors.append(f"bad call_scope: {event.get('call_scope')}")
        if event.get("phase") in {"completed", "failed", "rejected"} and not event.get("result_status"):
            errors.append("tool.call terminal phase needs result_status")
        if event.get("phase") == "requested" and event.get("result_status") not in (None, "pending"):
            errors.append("tool.call requested must have result_status=pending")
        if event.get("phase") in {"completed", "failed", "rejected"} and event.get("result_status") == "pending":
            errors.append("tool.call terminal phase cannot have result_status=pending")
        if event.get("phase") == "failed" and not event.get("error_code"):
            errors.append("tool.call failed needs error_code")
        if event.get("phase") == "rejected" and not event.get("reject_reason"):
            errors.append("tool.call rejected needs reject_reason")
        if "validity" in event and event.get("validity") not in VALIDITY:
            errors.append(f"bad validity: {event.get('validity')}")
        if event.get("validity") == "invalid" and not event.get("invalid_kind"):
            errors.append("invalid call needs invalid_kind")
        if "invalid_kind" in event and event.get("invalid_kind") not in INVALID_KINDS:
            errors.append(f"bad invalid_kind: {event.get('invalid_kind')}")
        if "tool_kind" in event and event.get("tool_kind") not in TOOL_KINDS:
            errors.append(f"bad tool_kind: {event.get('tool_kind')}")
        if "unnecessary" in event and not isinstance(event.get("unnecessary"), bool):
            errors.append("unnecessary must be boolean")
        if "unnecessary_kind" in event and event.get("unnecessary_kind") not in UNNECESSARY_KINDS:
            errors.append(f"bad unnecessary_kind: {event.get('unnecessary_kind')}")
        if "exit_code" in event and (
            isinstance(event.get("exit_code"), bool)
            or not isinstance(event.get("exit_code"), int)
        ):
            errors.append("exit_code must be an integer")
        if "duration_ms" in event and (
            isinstance(event.get("duration_ms"), bool)
            or not isinstance(event.get("duration_ms"), (int, float))
        ):
            errors.append("duration_ms must be a number")
        if "args_digest" in event and not isinstance(event.get("args_digest"), str):
            errors.append("args_digest must be a string")

    elif et == "tool.retry":
        for field in ("retry_of_call_id", "retry_scope", "retry_index", "retry_reason", "result_status"):
            if not event.get(field):
                errors.append(f"tool.retry missing {field}")
        if event.get("retry_scope") not in RETRY_SCOPES:
            errors.append(f"bad retry_scope: {event.get('retry_scope')}")
        index = event.get("retry_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            errors.append("tool.retry retry_index must be a positive integer")
        if event.get("retry_outcome") not in RETRY_OUTCOMES:
            errors.append(f"bad retry_outcome: {event.get('retry_outcome')}")
        if "recovered" in event and not isinstance(event.get("recovered"), bool):
            errors.append("recovered must be boolean")
        if "resolved_call_id" in event and not isinstance(event.get("resolved_call_id"), str):
            errors.append("resolved_call_id must be a string")

    elif et == "artifact.event":
        for field in ("op", "path"):
            if not event.get(field):
                errors.append(f"artifact.event missing {field}")
        if event.get("op") not in ARTIFACT_OPS:
            errors.append(f"bad artifact op: {event.get('op')}")
        if event.get("op") in {"create", "modify", "upload"} and not event.get("content_digest"):
            errors.append("artifact.event create/modify/upload needs content_digest")
        if "size" in event and (
            isinstance(event.get("size"), bool)
            or not isinstance(event.get("size"), int)
            or event.get("size") < 0
        ):
            errors.append("artifact size must be a non-negative integer")
        if "bom" in event and not isinstance(event.get("bom"), bool):
            errors.append("artifact bom must be boolean")

    elif et == "permission.event":
        for field in ("decision", "scope_type", "scope_value"):
            if not event.get(field):
                errors.append(f"permission.event missing {field}")
        if event.get("decision") not in PERMISSION_DECISIONS:
            errors.append(f"bad permission decision: {event.get('decision')}")
        if event.get("scope_type") not in SCOPE_TYPES:
            errors.append(f"bad scope_type: {event.get('scope_type')}")
        if "scope_value" in event and not isinstance(event.get("scope_value"), str):
            errors.append("scope_value must be a string")
        if "permission_id" in event and not isinstance(event.get("permission_id"), str):
            errors.append("permission_id must be a string")
        if "ui_action" in event and not isinstance(event.get("ui_action"), str):
            errors.append("ui_action must be a string")

    elif et == "user.input":
        if event.get("kind") not in USER_INPUT_KINDS:
            errors.append(f"bad user.input kind: {event.get('kind')}")
        count = event.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            errors.append("user.input count must be a positive integer")
        if "permission_ref" in event:
            if not isinstance(event.get("permission_ref"), str):
                errors.append("permission_ref must be a string")
            if event.get("kind") != "control":
                errors.append("permission_ref requires kind=control")
        if "initial_prompt" in event and not isinstance(event.get("initial_prompt"), bool):
            errors.append("initial_prompt must be boolean")

    elif et == "unknown.event":
        for field in ("unknown_kind", "reason"):
            if not event.get(field):
                errors.append(f"unknown.event missing {field}")
        if event.get("unknown_kind") not in UNKNOWN_KINDS:
            errors.append(f"bad unknown_kind: {event.get('unknown_kind')}")
        capabilities = event.get("affected_capabilities")
        single = event.get("affected_capability")
        if not capabilities and not single:
            errors.append("unknown.event missing affected_capability/affected_capabilities")
        if capabilities is not None and not (
            isinstance(capabilities, list)
            and all(isinstance(item, str) for item in capabilities)
        ):
            errors.append("affected_capabilities must be a list of strings")
        if event.get("estimated_count") is not None:
            errors.append("unknown.event estimated_count must be null")

    elif et == "evidence.gap":
        for field in ("gap_type", "impact"):
            if not event.get(field):
                errors.append(f"evidence.gap missing {field}")
        if event.get("gap_type") not in GAP_TYPES:
            errors.append(f"bad gap_type: {event.get('gap_type')}")
        if event.get("impact") not in IMPACTS:
            errors.append(f"bad impact: {event.get('impact')}")
        if not isinstance(event.get("affected_event_types"), list):
            errors.append("evidence.gap affected_event_types must be a list")
        if "affected_event_ids" in event and not (
            isinstance(event.get("affected_event_ids"), list)
            and all(isinstance(item, str) for item in event["affected_event_ids"])
        ):
            errors.append("affected_event_ids must be a list of strings")
        if "affected_seqs" in event and not (
            isinstance(event.get("affected_seqs"), list)
            and all(
                isinstance(item, int) and not isinstance(item, bool)
                for item in event["affected_seqs"]
            )
        ):
            errors.append("affected_seqs must be a list of integers")

    return errors


def validate_events(events: list[dict]) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    for index, event in enumerate(events, 1):
        for error in validate_event(event):
            errors.append(f"event {index}: {error}")

    run_ids = {event.get("run_id") for event in events if event.get("run_id")}
    if len(run_ids) > 1:
        errors.append(f"multiple run_id: {sorted(run_ids)}")
    session_ids = {
        event.get("session_id") for event in events if event.get("session_id")
    }
    if len(session_ids) > 1:
        errors.append(f"multiple session_id: {sorted(session_ids)}")
    if sum(1 for event in events if event.get("event_type") == "run.start") > 1:
        errors.append("multiple run.start events")
    if sum(1 for event in events if event.get("event_type") == "run.end") > 1:
        errors.append("multiple run.end events")

    ids = [event.get("event_id") for event in events]
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate event_id: {duplicates}")

    seqs = [event.get("seq") for event in events]
    if all(isinstance(seq, int) and not isinstance(seq, bool) for seq in seqs):
        if any(left >= right for left, right in zip(seqs, seqs[1:])):
            errors.append("seq must be strictly increasing")

    call_ids = {
        event.get("call_id")
        for event in events
        if event.get("event_type") == "tool.call" and event.get("call_id")
    }
    call_with_args: dict[str, bool] = {}
    for event in events:
        if event.get("event_type") != "tool.call" or not event.get("call_id"):
            continue
        has_args = bool(event.get("args_digest") or event.get("args_summary"))
        call_with_args[event.get("call_id")] = (
            call_with_args.get(event.get("call_id"), False) or has_args
        )
    for call_id, has_args in sorted(call_with_args.items()):
        if not has_args:
            errors.append(f"call {call_id} has no args on any event")
    for event in events:
        if event.get("event_type") == "tool.retry":
            ref = event.get("retry_of_call_id")
            if ref and ref not in call_ids:
                warnings.append(f"retry references unknown call_id: {ref}")

    retry_chains: dict[str, list[dict]] = {}
    for event in events:
        if event.get("event_type") == "tool.retry":
            key = event.get("retry_of_call_id")
            if key:
                retry_chains.setdefault(key, []).append(event)
    for key, chain in retry_chains.items():
        indices = [
            event.get("retry_index") for event in chain
            if isinstance(event.get("retry_index"), int)
        ]
        if len(indices) != len(set(indices)):
            warnings.append(f"duplicate retry_index for {key}")
        if indices and sorted(indices) != list(range(1, max(indices) + 1)):
            warnings.append(f"retry_index gap for {key}")

    if not any(event.get("event_type") == "run.start" for event in events):
        warnings.append("missing run.start")
    if not any(event.get("event_type") == "run.end" for event in events):
        warnings.append("missing run.end")

    return {"errors": errors, "warnings": warnings}


def parse_lines(
    lines,
    run_id: str = "synthetic",
    session_id: str = "s1",
    strict: bool = True,
    errors: list[str] | None = None,
) -> list[dict]:
    events: list[dict] = []
    for line_no, line in enumerate(lines, 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            event = dict(DEFAULTS)
            event.update(parse_line(raw))
            event.setdefault("run_id", run_id)
            event.setdefault("session_id", session_id)
            event.setdefault("seq", len(events) + 1)
            event.setdefault("event_id", f"EV-{event['run_id']}-{event['seq']:06d}")
            normalize_event(event)
        except ParseError as exc:
            message = f"line {line_no}: {exc}"
            if strict:
                raise ParseError(message) from exc
            if errors is not None:
                errors.append(message)
            continue
        events.append(event)

    if strict:
        report = validate_events(events)
        if report["errors"]:
            raise ParseError("; ".join(report["errors"]))
    return events


def _ts(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_invalid(event: dict) -> bool:
    return (
        event.get("validity") == "invalid"
        or event.get("invalid_kind") in INVALID_COUNT_KINDS
    )


def _has_output(event: dict) -> bool:
    return any(
        event.get(key)
        for key in ("output_digest", "stdout_digest", "stderr_digest", "output_ref")
    )


def _failure_signals(event: dict) -> list[str]:
    signals = []
    if event.get("phase") == "failed":
        signals.append("phase_failed")
    if event.get("result_status") in ("error", "failed"):
        signals.append("result_status_error")
    if event.get("exit_code", 0) not in (0, None):
        signals.append("exit_code_nonzero")
    return signals


def _args_key(event: dict) -> str | None:
    digest = event.get("args_digest")
    if digest:
        return f"digest:{digest}"
    summary = event.get("args_summary")
    if summary is not None:
        canonical = json.dumps(summary, sort_keys=True, ensure_ascii=False)
        return "summary:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return None


def _record_args_key(record: dict) -> str | None:
    for event in record["events"]:
        key = _args_key(event)
        if key is not None:
            return key
    return None


def _collapse_identities(
    events: list[dict],
    priority,
) -> tuple[dict[tuple, dict], dict[tuple, list[dict]]]:
    """Collapse same-identity events from multiple sources to one representative."""
    by_identity: dict[tuple, list[dict]] = {}
    for event in events:
        identity = (event.get("tool_name"), _args_key(event))
        by_identity.setdefault(identity, []).append(event)
    for identity in list(by_identity):
        tool_name, args_key = identity
        if args_key is not None:
            continue
        candidates = [
            key for key in by_identity
            if key[0] == tool_name and key[1] is not None
        ]
        if len(candidates) == 1:
            by_identity[candidates[0]] = (
                by_identity[candidates[0]] + by_identity.pop(identity)
            )
    representatives = {
        identity: max(group, key=priority)
        for identity, group in by_identity.items()
    }
    return representatives, by_identity


def _make_call_record(
    call_id: str,
    requested: dict | None,
    terminal: dict | None,
    events_group: list[dict],
) -> dict:
    sources = {event.get("source") for event in events_group if event.get("source")}
    base = requested or terminal or events_group[0]
    return {
        "key": call_id,
        "call_id": call_id,
        "tool_name": base.get("tool_name"),
        "args_key": _args_key(base),
        "requested": requested,
        "terminal": terminal,
        "base": base,
        "events": events_group,
        "sources": sources,
        "collision": len(sources) > 1,
    }


def _group_calls(
    call_events: list[dict],
    authoritative_source: str | None = None,
) -> list[dict]:
    """Group tool.call events by call_id and pair requested with terminal.

    A single requested + single terminal under one call_id is always paired,
    even when one side uses args_digest and the other args_summary. Only
    repeated phases with different identities are split into separate calls.
    """
    grouped: dict[str, list[dict]] = {}
    for event in call_events:
        call_id = event.get("call_id") or event.get("event_id") or f"anon-{id(event)}"
        grouped.setdefault(call_id, []).append(event)

    def priority(event: dict) -> int:
        if authoritative_source and event.get("source") == authoritative_source:
            return 100
        return SOURCE_PRIORITY.get(event.get("source"), 0)

    records = []
    for call_id, group in grouped.items():
        requested_events = [e for e in group if e.get("phase") == "requested"]
        terminal_events = [
            e for e in group if e.get("phase") in ("completed", "failed", "rejected")
        ]
        requested_reps, requested_groups = _collapse_identities(
            requested_events, priority
        )
        terminal_reps, terminal_groups = _collapse_identities(
            terminal_events, priority
        )

        pairs: list[tuple[tuple, tuple]] = []
        matched_requested: set[tuple] = set()
        matched_terminal: set[tuple] = set()

        for req_identity in requested_reps:
            if req_identity in terminal_reps:
                pairs.append((req_identity, req_identity))
                matched_requested.add(req_identity)
                matched_terminal.add(req_identity)

        for req_identity in requested_reps:
            if req_identity in matched_requested:
                continue
            candidates = [
                term_identity
                for term_identity in terminal_reps
                if term_identity not in matched_terminal
                and term_identity[0] == req_identity[0]
            ]
            if len(candidates) == 1:
                pairs.append((req_identity, candidates[0]))
                matched_requested.add(req_identity)
                matched_terminal.add(candidates[0])

        call_records = []
        for req_identity, term_identity in pairs:
            events_group = (
                requested_groups.get(req_identity, [])
                + terminal_groups.get(term_identity, [])
            )
            call_records.append(
                _make_call_record(
                    call_id,
                    requested_reps[req_identity],
                    terminal_reps[term_identity],
                    events_group,
                )
            )
        for req_identity, group_events in requested_groups.items():
            if req_identity in matched_requested:
                continue
            call_records.append(
                _make_call_record(
                    call_id, requested_reps[req_identity], None, group_events
                )
            )
        for term_identity, group_events in terminal_groups.items():
            if term_identity in matched_terminal:
                continue
            call_records.append(
                _make_call_record(
                    call_id, None, terminal_reps[term_identity], group_events
                )
            )

        if not call_records:
            call_records.append(_make_call_record(call_id, None, None, group))

        reused = len(call_records) > 1
        for index, record in enumerate(call_records, 1):
            record["reused"] = reused
            record["key"] = call_id if not reused else f"{call_id}#{index}"
        records.extend(call_records)
    return records


def summarize(events: list[dict], manifest: dict | None = None) -> dict:
    manifest = manifest or {}
    run_ids = {e.get("run_id") for e in events if e.get("run_id")}
    if len(run_ids) > 1:
        raise ParseError(f"multiple run_id in one event stream: {sorted(run_ids)}")
    session_ids = {e.get("session_id") for e in events if e.get("session_id")}
    if len(session_ids) > 1:
        raise ParseError(f"multiple session_id in one event stream: {sorted(session_ids)}")
    if sum(1 for e in events if e.get("event_type") == "run.start") > 1:
        raise ParseError("multiple run.start events in one event stream")
    if sum(1 for e in events if e.get("event_type") == "run.end") > 1:
        raise ParseError("multiple run.end events in one event stream")
    run_start = next((e for e in events if e.get("event_type") == "run.start"), {})
    run_end = next((e for e in events if e.get("event_type") == "run.end"), {})
    authoritative_source = (
        manifest.get("authoritative_source")
        or run_start.get("authoritative_source")
    )

    top_events = [
        e for e in events
        if e.get("event_type") == "tool.call"
        and e.get("call_scope") == "top_level"
    ]
    sub_events = [
        e for e in events
        if e.get("event_type") == "tool.call"
        and e.get("call_scope") == "subcall"
    ]
    call_records = _group_calls(top_events, authoritative_source)
    sub_records = _group_calls(sub_events, authoritative_source)
    retries_internal = [
        e for e in events
        if e.get("event_type") == "tool.retry" and e.get("retry_scope") == "internal"
    ]
    retries_top = [
        e for e in events
        if e.get("event_type") == "tool.retry" and e.get("retry_scope") == "top_level"
    ]
    retries_all = [e for e in events if e.get("event_type") == "tool.retry"]
    failures = []
    failure_signal_counts = Counter()
    rejected = []
    for record in call_records:
        terminal = record["terminal"]
        if terminal is None:
            continue
        signals = _failure_signals(terminal)
        if signals:
            failures.append(record)
            failure_signal_counts.update(signals)
        if terminal.get("phase") == "rejected":
            rejected.append(record)

    retry_related_ids = set()
    for event in retries_all:
        if event.get("retry_of_call_id"):
            retry_related_ids.add(event.get("retry_of_call_id"))
        if event.get("resolved_call_id"):
            retry_related_ids.add(event.get("resolved_call_id"))

    seen, duplicates = {}, []
    duplicate_check_skipped = 0
    duplicate_retry_excluded = 0
    for record in call_records:
        if record["key"] in retry_related_ids or record["call_id"] in retry_related_ids:
            duplicate_retry_excluded += 1
            continue
        args_key = _record_args_key(record)
        if args_key is None:
            duplicate_check_skipped += 1
            continue
        key = (record.get("tool_name"), args_key)
        if key in seen:
            duplicates.append(record["key"])
        else:
            seen[key] = record["key"]
    duplicate_check_complete = duplicate_check_skipped == 0
    call_id_collision_ids = [
        record["key"] for record in call_records if record["collision"]
    ]
    call_id_collisions = len(call_id_collision_ids)
    deduplicated_call_events = sum(
        len(record["events"]) - 1 for record in call_records
    )

    permission = {"requested": 0, "granted": 0, "denied": 0, "blocked": 0, "allowed": 0}
    for event in events:
        if event.get("event_type") == "permission.event":
            decision = event.get("decision")
            if decision in permission:
                permission[decision] += 1

    user_input_events = [
        event for event in events if event.get("event_type") == "user.input"
    ]

    def _sum_kind(kind: str) -> int:
        return sum(
            event.get("count", 1)
            for event in user_input_events
            if event.get("kind") == kind and not event.get("initial_prompt")
        )

    initial_prompts = sum(
        event.get("count", 1)
        for event in user_input_events
        if event.get("initial_prompt")
    )
    semantic_inputs = _sum_kind("semantic")
    mechanical_continues = _sum_kind("mechanical")
    control_actions = _sum_kind("control")
    user_inputs_total = (
        semantic_inputs + mechanical_continues + control_actions + initial_prompts
    )
    permission_responses = sum(
        1 for event in user_input_events if event.get("permission_ref")
    )

    permission_events = [
        event for event in events if event.get("event_type") == "permission.event"
    ]
    permission_response_refs = {
        event.get("permission_ref")
        for event in user_input_events
        if event.get("permission_ref")
    }
    resolved_permission_ids = {
        event.get("permission_id")
        for event in permission_events
        if event.get("permission_id")
        and event.get("decision") in ("granted", "denied", "blocked", "allowed")
    }
    permission_pending = 0
    for event in permission_events:
        if event.get("decision") == "requested":
            permission_id = event.get("permission_id")
            if (
                not permission_id
                or (
                    permission_id not in resolved_permission_ids
                    and permission_id not in permission_response_refs
                )
            ):
                permission_pending += 1

    unknown_events = [e for e in events if e.get("event_type") == "unknown.event"]
    log_complete = bool(manifest.get("log_complete", run_start.get("log_complete", False)))
    call_keys = {record["key"] for record in call_records} | {
        record["call_id"] for record in call_records
    }
    unlinked_top_retries = [
        event for event in retries_top
        if not event.get("resolved_call_id")
        or event.get("resolved_call_id") not in call_keys
    ]
    tool_calls_from_top_level_retry = len(unlinked_top_retries)
    tool_calls_visible = (
        sum(
            1 for record in call_records
            if record["base"].get("observability") in ("visible", "partial")
        )
        + sum(
            1 for event in unlinked_top_retries
            if event.get("observability") in ("visible", "partial")
        )
    )
    tool_calls_missing_request = sum(
        1 for record in call_records if record["requested"] is None
    )
    tool_calls_missing_terminal = sum(
        1 for record in call_records if record["terminal"] is None
    )
    tool_calls_incomplete = sum(
        1 for record in call_records
        if record["requested"] is None or record["terminal"] is None
    )
    unknown_observability_events = [
        event for event in events
        if event.get("event_type") in (
            "tool.call", "tool.retry", "artifact.event", "permission.event"
        )
        and event.get("observability") == "unknown"
    ]
    complete_visibility = (
        log_complete
        and not unknown_events
        and tool_calls_incomplete == 0
        and not unknown_observability_events
        and (len(call_records) == 0 or tool_calls_visible > 0)
    )
    if complete_visibility:
        tool_calls_visibility = "complete"
    elif tool_calls_visible > 0:
        tool_calls_visibility = "partial"
    else:
        tool_calls_visibility = "unknown"
    unknown_calls = 0 if tool_calls_visibility == "complete" else None
    tool_calls_partial = tool_calls_visibility == "partial"
    derived_counts_complete = tool_calls_visibility == "complete"

    capabilities = manifest.get("capabilities", run_start.get("capabilities", {})) or {}
    na_not_applicable = sorted(k for k, v in capabilities.items() if v == "not_applicable")
    na_unknown = set()
    na_unknown_invalid = set()
    for event in unknown_events:
        capabilities_list = event.get("affected_capabilities")
        if not capabilities_list and event.get("affected_capability"):
            capabilities_list = [event.get("affected_capability")]
        for capability in capabilities_list or []:
            if capability in RUBRIC_SUBITEMS:
                na_unknown.add(capability)
            else:
                na_unknown_invalid.add(capability)
    na_unknown = sorted(na_unknown)
    na_unknown_invalid = sorted(na_unknown_invalid)

    trace_events = [
        event for event in events
        if event.get("event_type") in (
            "tool.call", "tool.retry", "artifact.event", "permission.event"
        )
    ]
    product_trace = [
        event for event in trace_events if event.get("source") == "product_log"
    ]
    reconstructed_trace = [
        event for event in trace_events
        if event.get("source") in ("manual", "evaluator", "terminal_capture", "ui_capture")
    ]
    explicit_origin = manifest.get("trace_origin") or run_start.get("trace_origin")
    if product_trace and reconstructed_trace:
        observed_origin = "mixed"
    elif product_trace:
        observed_origin = "product_exported"
    else:
        observed_origin = "evaluator_reconstructed"
    trace_origin = observed_origin
    trace_origin_conflict = bool(
        explicit_origin in TRACE_ORIGINS and explicit_origin != observed_origin
    )
    trace_total = len(trace_events)
    product_log_share = len(product_trace) / trace_total if trace_total else 0.0
    h8_scorable = product_log_share >= H8_PRODUCT_LOG_THRESHOLD
    h8_reason = (
        None if h8_scorable
        else f"H8 product_log_share={product_log_share:.2f} < {H8_PRODUCT_LOG_THRESHOLD}"
    )

    tool_call_events = [
        event for event in events if event.get("event_type") == "tool.call"
    ]
    product_tool_calls = [
        event for event in tool_call_events if event.get("source") == "product_log"
    ]
    h8_1_share = (
        len(product_tool_calls) / len(tool_call_events) if tool_call_events else 0.0
    )
    h8_1_scorable = bool(tool_call_events) and h8_1_share >= H8_PRODUCT_LOG_THRESHOLD

    failed_call_events = [
        event for event in tool_call_events if _failure_signals(event)
    ]
    status_call_events = [
        event for event in tool_call_events
        if event.get("phase") in ("completed", "failed", "rejected")
        and event.get("result_status")
    ]
    h8_2_evidence_events = []
    seen_evidence = set()
    for event in failed_call_events + status_call_events:
        marker = id(event)
        if marker not in seen_evidence:
            seen_evidence.add(marker)
            h8_2_evidence_events.append(event)
    product_status_evidence = [
        event for event in h8_2_evidence_events
        if event.get("source") == "product_log"
    ]
    h8_2_share = (
        len(product_status_evidence) / len(h8_2_evidence_events)
        if h8_2_evidence_events else 0.0
    )
    h8_2_scorable = (
        bool(h8_2_evidence_events)
        and h8_2_share >= H8_PRODUCT_LOG_THRESHOLD
    )

    artifact_events = [
        event for event in events if event.get("event_type") == "artifact.event"
    ]
    product_artifacts = [
        event for event in artifact_events if event.get("source") == "product_log"
    ]
    h8_3_share = (
        len(product_artifacts) / len(artifact_events) if artifact_events else 0.0
    )
    workspace_hashes_present = bool(
        run_start.get("workspace_pre_hash") and run_end.get("workspace_post_hash")
    )
    h8_3_scorable = (
        bool(artifact_events)
        and h8_3_share >= H8_PRODUCT_LOG_THRESHOLD
        and workspace_hashes_present
    )
    h8_subitem_scorable = {
        "H8.1": h8_1_scorable,
        "H8.2": h8_2_scorable,
        "H8.3": h8_3_scorable,
    }
    h8_subitem_reasons = {
        "H8.1": None if h8_1_scorable else f"product_log tool.call share={h8_1_share:.2f}",
        "H8.2": None if h8_2_scorable else (
            "no error/status records"
            if not h8_2_evidence_events
            else f"product_log error/status share={h8_2_share:.2f}"
        ),
        "H8.3": None if h8_3_scorable else (
            f"product_log artifact share={h8_3_share:.2f}, "
            f"workspace_hashes_present={workspace_hashes_present}"
        ),
    }

    product_log_coverage = sorted({
        event.get("event_type") for event in product_trace if event.get("event_type")
    })
    coverage_source_breakdown: dict[str, Counter] = {}
    for event in events:
        event_type = event.get("event_type")
        source = event.get("source")
        if event_type and source:
            coverage_source_breakdown.setdefault(event_type, Counter())[source] += 1
    coverage_source_breakdown = {
        event_type: dict(counter)
        for event_type, counter in coverage_source_breakdown.items()
    }

    grade_rank = {"E1": 1, "E2": 2, "E3": 3, "E4": 4}
    graded_events = [
        event for event in events if event.get("evidence_grade") in grade_rank
    ]
    evidence_grade_min = (
        max(graded_events, key=lambda event: grade_rank[event["evidence_grade"]])[
            "evidence_grade"
        ]
        if graded_events else None
    )
    evidence_grade_by_source: dict[str, Counter] = {}
    for event in graded_events:
        source = event.get("source")
        if source:
            evidence_grade_by_source.setdefault(source, Counter())[
                event["evidence_grade"]
            ] += 1
    evidence_grade_by_source = {
        source: dict(counter) for source, counter in evidence_grade_by_source.items()
    }

    parsed_stamps = []
    for event in events:
        stamp_value = event.get("ts")
        if stamp_value:
            try:
                parsed_stamps.append(_parse_stamp(stamp_value))
            except ValueError:
                pass
    mixed_timezone = (
        any(stamp.tzinfo is None for stamp in parsed_stamps)
        and any(stamp.tzinfo is not None for stamp in parsed_stamps)
    )

    after_end = 0
    reference_tz = None
    if run_end.get("ts"):
        try:
            end_stamp = _parse_stamp(run_end["ts"])
        except ValueError:
            end_stamp = None
        if end_stamp is not None:
            reference_tz = end_stamp.tzinfo
            if reference_tz is None and run_start.get("ts"):
                try:
                    start_stamp = _parse_stamp(run_start["ts"])
                    if start_stamp.tzinfo is not None:
                        reference_tz = start_stamp.tzinfo
                except ValueError:
                    pass
            unresolved = False
            for record in call_records:
                stamp_value = record["base"].get("ts")
                if not stamp_value:
                    continue
                try:
                    call_stamp = _parse_stamp(stamp_value)
                except ValueError:
                    unresolved = True
                    continue
                if call_stamp.tzinfo is None:
                    if reference_tz is None:
                        unresolved = True
                        continue
                    call_stamp = call_stamp.replace(tzinfo=reference_tz)
                if end_stamp.tzinfo is None and call_stamp.tzinfo is not None:
                    unresolved = True
                    continue
                if call_stamp > end_stamp:
                    after_end += 1
            if unresolved:
                after_end = None

    tool_calls_total = len(call_records) + tool_calls_from_top_level_retry
    tool_calls_invalid_observed = sum(
        1 for record in call_records if _is_invalid(record["base"])
    )
    tool_calls_invalid_unknown = sum(
        1 for record in call_records
        if not record["base"].get("validity")
        and not record["base"].get("invalid_kind")
    )
    tool_calls_unnecessary_observed = sum(
        1 for record in call_records if record["base"].get("unnecessary") is True
    )
    tool_calls_unnecessary_unknown = sum(
        1 for record in call_records if "unnecessary" not in record["base"]
    )
    tool_calls_invalid = (
        tool_calls_invalid_observed if tool_calls_visibility == "complete" else None
    )
    tool_calls_unnecessary = (
        tool_calls_unnecessary_observed if tool_calls_visibility == "complete" else None
    )

    command_records = [
        record for record in call_records
        if record["base"].get("tool_kind") == "command"
    ]
    command_missing_exit = 0
    command_missing_output = 0
    for record in command_records:
        terminal_event = record["terminal"]
        if terminal_event is None:
            command_missing_exit += 1
            command_missing_output += 1
            continue
        if terminal_event.get("exit_code") is None:
            command_missing_exit += 1
        if not _has_output(terminal_event):
            command_missing_output += 1
    command_calls_count = (
        len(command_records) if tool_calls_visibility == "complete" else None
    )
    command_missing_exit_out = (
        command_missing_exit if tool_calls_visibility == "complete" else None
    )
    command_missing_output_out = (
        command_missing_output if tool_calls_visibility == "complete" else None
    )

    timeout_source = run_end.get("timeout_source")
    timeout_ts = run_end.get("timeout_ts")
    if not timeout_ts and run_end.get("terminate_reason") in ("timeout", "budget"):
        timeout_ts = run_end.get("end_ts")
    tool_calls_after_timeout = 0
    if timeout_ts:
        try:
            cutoff = _parse_stamp(timeout_ts)
        except ValueError:
            cutoff = None
        if cutoff is not None:
            cutoff_tz = cutoff.tzinfo or reference_tz
            unresolved_timeout = False
            timeout_count = 0
            for record in call_records:
                stamp_value = record["base"].get("ts")
                if not stamp_value:
                    continue
                try:
                    call_stamp = _parse_stamp(stamp_value)
                except ValueError:
                    unresolved_timeout = True
                    continue
                if call_stamp.tzinfo is None:
                    if cutoff_tz is None:
                        unresolved_timeout = True
                        continue
                    call_stamp = call_stamp.replace(tzinfo=cutoff_tz)
                if cutoff.tzinfo is None and call_stamp.tzinfo is not None:
                    unresolved_timeout = True
                    continue
                if call_stamp > cutoff:
                    timeout_count += 1
            tool_calls_after_timeout = (
                None if unresolved_timeout else timeout_count
            )
        else:
            tool_calls_after_timeout = None
    elif run_end.get("terminate_reason") in ("timeout", "budget"):
        tool_calls_after_timeout = None

    artifacts = [e for e in events if e.get("event_type") == "artifact.event"]
    artifact_count = len(artifacts)
    artifacts_without_call_id = [
        e.get("path") for e in artifacts if not e.get("call_id")
    ]
    artifacts_missing_digest = [
        e.get("path") for e in artifacts
        if e.get("op") in ("create", "modify") and not e.get("content_digest")
    ]

    workspace_pre_hash = run_start.get("workspace_pre_hash")
    workspace_post_hash = run_end.get("workspace_post_hash")
    workspace_hash_complete = bool(workspace_pre_hash and workspace_post_hash)
    output_hashes = run_end.get("output_hashes") or []
    evidence_gap_details = [
        {
            "gap_type": e.get("gap_type"),
            "affected_event_types": e.get("affected_event_types", []),
            "affected_event_ids": e.get("affected_event_ids", []),
            "affected_seqs": e.get("affected_seqs", []),
            "impact": e.get("impact"),
            "raw_ref": e.get("raw_ref"),
        }
        for e in events if e.get("event_type") == "evidence.gap"
    ]

    tool_failure_signals = sum(failure_signal_counts.values())
    retries_recovered = sum(
        1 for event in retries_all
        if event.get("retry_outcome") == "recovered" or event.get("recovered") is True
    )
    retries_failed = sum(
        1 for event in retries_all if event.get("retry_outcome") == "failed"
    )
    retries_unknown = sum(
        1 for event in retries_all
        if event.get("retry_outcome") not in ("recovered", "failed")
    )

    retry_chains: dict[str, list[dict]] = {}
    for event in retries_all:
        key = event.get("retry_of_call_id") or "unknown"
        retry_chains.setdefault(key, []).append(event)
    retry_chain_incomplete = 0
    retry_chain_recovered = 0
    retry_chain_failed = 0
    retry_chain_unknown = 0
    retry_chain_details = []
    for key, chain in retry_chains.items():
        indices = [
            event.get("retry_index") for event in chain
            if isinstance(event.get("retry_index"), int)
        ]
        complete = bool(indices) and sorted(indices) == list(range(1, max(indices) + 1))
        if not complete:
            retry_chain_incomplete += 1
        last = max(chain, key=lambda event: event.get("retry_index", 0))
        outcome = last.get("retry_outcome")
        if outcome == "recovered":
            retry_chain_recovered += 1
        elif outcome == "failed":
            retry_chain_failed += 1
        else:
            retry_chain_unknown += 1
        retry_chain_details.append({
            "retry_of_call_id": key,
            "retry_indices": sorted(indices),
            "complete": complete,
            "last_outcome": outcome,
            "last_resolved_call_id": last.get("resolved_call_id"),
        })

    warnings = []
    event_ids = [e.get("event_id") for e in events]
    if len(event_ids) != len(set(event_ids)):
        warnings.append("duplicate event_id")
    seqs = [e.get("seq") for e in events]
    if any(
        isinstance(left, int) and isinstance(right, int) and left >= right
        for left, right in zip(seqs, seqs[1:])
    ):
        warnings.append("seq is not strictly increasing")

    for record in call_records:
        requested = record["requested"]
        base = record["base"]
        if requested is None:
            warnings.append(f"terminal-only call without requested event: {record['key']}")
        if record["terminal"] is None:
            warnings.append(f"requested call without terminal phase: {record['key']}")
        if not base.get("args_digest"):
            warnings.append(f"missing args_digest: {record['key']}")
    if tool_calls_partial:
        warnings.append("tool call visibility is partial")
    if tool_calls_visibility == "unknown":
        warnings.append("tool call visibility is unknown")
    if not derived_counts_complete:
        warnings.append("derived counts are observed-only")
    if after_end:
        warnings.append(f"{after_end} tool.call event(s) after run.end")
    for event in unknown_events:
        if event.get("estimated_count") is not None:
            warnings.append("unknown.event estimated_count must be null")
    if run_end.get("terminate_reason") in ("timeout", "budget") and not timeout_source:
        warnings.append("timeout without timeout_source")
    if workspace_pre_hash and not workspace_post_hash:
        warnings.append("missing workspace_post_hash")
    if workspace_post_hash and not workspace_pre_hash:
        warnings.append("missing workspace_pre_hash")
    if not output_hashes:
        warnings.append("missing output_hashes")
    if artifacts_without_call_id:
        warnings.append(f"artifact(s) without call_id: {artifacts_without_call_id}")
    if duplicate_check_skipped:
        warnings.append(
            f"{duplicate_check_skipped} call(s) skipped duplicate check: missing args key"
        )
    if call_id_collisions:
        warnings.append(f"call_id collisions across sources: {call_id_collision_ids}")
    if any(record["reused"] for record in call_records):
        warnings.append(
            "reused call_id with different tool/args: "
            f"{sorted({record['call_id'] for record in call_records if record['reused']})}"
        )
    if mixed_timezone:
        warnings.append("mixed naive/aware timestamps; time-based counts may be null")
    if trace_origin_conflict:
        warnings.append("trace_origin conflicts with observed sources; using observed origin")
    if tool_calls_invalid_unknown:
        warnings.append(f"{tool_calls_invalid_unknown} tool call(s) with unknown validity")
    if tool_calls_unnecessary_unknown:
        warnings.append(
            f"{tool_calls_unnecessary_unknown} tool call(s) with unknown unnecessary flag"
        )
    if permission_pending:
        warnings.append(f"{permission_pending} permission request(s) pending without decision")
    permission_ids = {
        event.get("permission_id")
        for event in permission_events
        if event.get("permission_id")
    }
    for event in permission_events:
        if event.get("decision") == "requested" and not event.get("permission_id"):
            warnings.append("permission request without permission_id")
    for event in user_input_events:
        ref = event.get("permission_ref")
        if ref and ref not in permission_ids:
            warnings.append(f"permission_ref without matching permission_id: {ref}")
    if na_unknown_invalid:
        warnings.append(f"unknown capabilities not in rubric: {na_unknown_invalid}")
    if not h8_scorable:
        warnings.append("H8 trace is not product-exported")
    if run_end.get("evaluator_fallback") and run_end.get("completion_at_terminate") is None:
        warnings.append("evaluator fallback without completion_at_terminate")

    complete_counts = tool_calls_visibility == "complete"
    tool_failures_out = len(failures) if complete_counts else None
    duplicate_count_out = len(duplicates) if complete_counts else None
    rejected_count_out = len(rejected) if complete_counts else None
    if permission_events or complete_counts:
        permission_out = permission
        permission_pending_out = permission_pending
    else:
        permission_out = {key: None for key in permission}
        permission_pending_out = None
    permission_counts_complete = bool(log_complete) and not any(
        event.get("observability") == "unknown" for event in permission_events
    )
    call_id_reuse_ids = sorted({
        record["call_id"] for record in call_records if record["reused"]
    })

    return {
        "run_id": run_start.get("run_id") or (events[0].get("run_id") if events else None),
        "session_id": run_start.get("session_id") or (events[0].get("session_id") if events else None),
        "authoritative_source": authoritative_source,
        "capture_source": run_start.get("capture_source"),
        "observability_tier": run_start.get("observability_tier"),
        "evidence_grade": run_start.get("evidence_grade"),
        "trace_origin": trace_origin,
        "trace_origin_observed": observed_origin,
        "trace_origin_conflict": trace_origin_conflict,
        "product_log_share": product_log_share,
        "product_log_events": len(product_trace),
        "reconstructed_events": len(reconstructed_trace),
        "h8_scorable": h8_scorable,
        "h8_reason": h8_reason,
        "h8_subitem_scorable": h8_subitem_scorable,
        "h8_subitem_reasons": h8_subitem_reasons,
        "product_log_coverage": product_log_coverage,
        "evidence_grade_min": evidence_grade_min,
        "evidence_grade_by_source": evidence_grade_by_source,
        "unknown_observability_events": len(unknown_observability_events),
        "event_count": len(events),
        "tool_calls_total": tool_calls_total,
        "tool_calls_visible": tool_calls_visible,
        "tool_calls_from_top_level_retry": tool_calls_from_top_level_retry,
        "tool_calls_subcall": len(sub_records),
        "tool_calls_incomplete": tool_calls_incomplete,
        "tool_calls_missing_request": tool_calls_missing_request,
        "tool_calls_missing_terminal": tool_calls_missing_terminal,
        "tool_calls_visibility": tool_calls_visibility,
        "tool_calls_partial": tool_calls_partial,
        "derived_counts_complete": derived_counts_complete,
        "tool_calls_invalid": tool_calls_invalid,
        "tool_calls_invalid_unknown": tool_calls_invalid_unknown,
        "observed_tool_calls_invalid": tool_calls_invalid_observed,
        "tool_calls_unnecessary": tool_calls_unnecessary,
        "tool_calls_unnecessary_unknown": tool_calls_unnecessary_unknown,
        "observed_tool_calls_unnecessary": tool_calls_unnecessary_observed,
        "tool_calls_after_timeout": tool_calls_after_timeout,
        "retry_internal": len(retries_internal),
        "retry_top_level": len(retries_top),
        "retries_recovered": retries_recovered,
        "retries_failed": retries_failed,
        "retries_unknown": retries_unknown,
        "retry_chains": len(retry_chains),
        "retry_chain_incomplete": retry_chain_incomplete,
        "retry_chain_recovered": retry_chain_recovered,
        "retry_chain_failed": retry_chain_failed,
        "retry_chain_unknown": retry_chain_unknown,
        "retry_chain_details": retry_chain_details,
        "tool_failures": tool_failures_out,
        "observed_tool_failures": len(failures),
        "tool_failure_signals": tool_failure_signals,
        "failure_signal_counts": dict(failure_signal_counts),
        "tool_calls_duplicate": duplicate_count_out,
        "observed_tool_calls_duplicate": len(duplicates),
        "duplicate_check_complete": duplicate_check_complete,
        "duplicate_check_skipped": duplicate_check_skipped,
        "duplicate_retry_excluded": duplicate_retry_excluded,
        "duplicate_call_ids": duplicates,
        "call_id_collisions": call_id_collisions,
        "call_id_collision_ids": call_id_collision_ids,
        "call_id_reuse": len(call_id_reuse_ids),
        "call_id_reuse_ids": call_id_reuse_ids,
        "deduplicated_call_events": deduplicated_call_events,
        "permission_requested": permission_out["requested"],
        "permission_granted": permission_out["granted"],
        "permission_denied": permission_out["denied"],
        "permission_blocked": permission_out["blocked"],
        "permission_allowed": permission_out["allowed"],
        "permission_pending": permission_pending_out,
        "permission_counts_complete": permission_counts_complete,
        "observed_permission_requested": permission["requested"],
        "observed_permission_granted": permission["granted"],
        "observed_permission_denied": permission["denied"],
        "observed_permission_blocked": permission["blocked"],
        "observed_permission_allowed": permission["allowed"],
        "observed_permission_pending": permission_pending,
        "rejected_calls": rejected_count_out,
        "observed_rejected_calls": len(rejected),
        "unknown_events": len(unknown_events),
        "tool_calls_unknown": unknown_calls,
        "unknown_calls": unknown_calls,
        "user_inputs": user_inputs_total,
        "user_input_events": len(user_input_events),
        "initial_prompts": initial_prompts,
        "semantic_inputs": semantic_inputs,
        "mechanical_continues": mechanical_continues,
        "control_actions": control_actions,
        "permission_responses": permission_responses,
        "terminate_reason": run_end.get("terminate_reason"),
        "completion_at_terminate": run_end.get("completion_at_terminate"),
        "timeout_hit": run_end.get("terminate_reason") in ("timeout", "budget"),
        "timeout_source": timeout_source,
        "timeout_ts": timeout_ts,
        "product_timeout_hit": timeout_source == "product",
        "evaluator_timeout_hit": timeout_source == "evaluator",
        "evaluator_fallback": bool(run_end.get("evaluator_fallback", False)),
        "calls_after_end": after_end,
        "mixed_timezone": mixed_timezone,
        "workspace_pre_hash": workspace_pre_hash,
        "workspace_post_hash": workspace_post_hash,
        "workspace_hash_complete": workspace_hash_complete,
        "output_hashes": output_hashes,
        "output_hash_count": len(output_hashes),
        "artifact_count": artifact_count,
        "artifacts_without_call_id": artifacts_without_call_id,
        "artifacts_missing_digest": artifacts_missing_digest,
        "command_calls": command_calls_count,
        "command_calls_missing_exit_code": command_missing_exit_out,
        "command_calls_missing_output": command_missing_output_out,
        "observed_command_calls": len(command_records),
        "observed_command_calls_missing_exit_code": command_missing_exit,
        "observed_command_calls_missing_output": command_missing_output,
        "na_not_applicable": na_not_applicable,
        "na_unknown": na_unknown,
        "na_unknown_invalid": na_unknown_invalid,
        "evidence_gaps": [
            e.get("gap_type") for e in events if e.get("event_type") == "evidence.gap"
        ],
        "evidence_gap_details": evidence_gap_details,
        "coverage": sorted({e.get("event_type") for e in events if e.get("event_type")}),
        "coverage_source_breakdown": coverage_source_breakdown,
        "warnings": warnings,
    }
