"""rubric-v1.0 scoring engine: events + verdict + workspace diff -> 27 subitem scores.

Input contract:
  events          obs-v1 event list (JSONL read by caller)
  verdict         checker verdict JSON
  workspace_diff  workspace_diff.json from obs.workspace
  run_record      per-run metadata (task_meta.json + run fields)

Output contract: score-v1 JSON with one item per rubric subitem, dimension
scores on fixed shares, and N/A reasons. No product judgement.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from obs.parser import _group_calls, summarize

SCORE_SCHEMA = "score-v1"

WEIGHTS = {
    "H1": 15, "H2": 13, "H3": 12, "H4": 15,
    "H5": 13, "H6": 13, "H7": 12, "H8": 7,
}

SUBITEMS = {
    "H1": ["H1.1", "H1.2", "H1.3", "H1.4"],
    "H2": ["H2.1", "H2.2", "H2.3"],
    "H3": ["H3.1", "H3.2", "H3.3"],
    "H4": ["H4.1", "H4.2", "H4.3", "H4.4"],
    "H5": ["H5.1", "H5.2", "H5.3"],
    "H6": ["H6.1", "H6.2", "H6.3"],
    "H7": ["H7.1", "H7.2", "H7.3", "H7.4"],
    "H8": ["H8.1", "H8.2", "H8.3"],
}

STOP_SET = {
    "assertions_passed", "timeout", "budget",
    "unrecoverable_error", "safety_stop",
}

FAILURE_STATUS = {"error", "failed"}


def _norm_path(value) -> str:
    if not value:
        return ""
    text = str(value).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _args_key(event: dict):
    digest = event.get("args_digest")
    if digest:
        return f"digest:{digest}"
    args = event.get("args_summary")
    if args is not None:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return "summary:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return None


def _failure_signals(event: dict) -> list[str]:
    signals = []
    if event.get("phase") == "failed":
        signals.append("phase_failed")
    if event.get("result_status") in ("error", "failed"):
        signals.append("result_status_error")
    if event.get("exit_code", 0) not in (0, None):
        signals.append("exit_code_nonzero")
    return signals


def _has_output(event: dict) -> bool:
    return any(
        event.get(key)
        for key in ("output_digest", "stdout_digest", "stderr_digest", "output_ref")
    )


def _ids(events, limit=5) -> list[str]:
    return [e.get("event_id") for e in events if e.get("event_id")][:limit]


def _item(subitem, score, reason, evidence=None, na="none") -> dict:
    return {
        "subitem": subitem,
        "score": score,
        "na": na,
        "reason": reason,
        "evidence": evidence or [],
    }


def _two(subitem, reason, evidence=None):
    return _item(subitem, 2, reason, evidence)


def _one(subitem, reason, evidence=None):
    return _item(subitem, 1, reason, evidence)


def _zero(subitem, reason, evidence=None):
    return _item(subitem, 0, reason, evidence)


def _na_unknown(subitem, reason, evidence=None):
    return _item(subitem, None, reason, evidence, na="unknown")


def _na_na(subitem, reason, evidence=None):
    return _item(subitem, None, reason, evidence, na="not_applicable")


class _Facts:
    def __init__(self, events, verdict, workspace_diff, run_record):
        self.events = events
        self.verdict = verdict or {}
        self.diff = workspace_diff or {}
        self.record = run_record or {}
        self.summary = summarize(events)
        self.form = self.record.get("form", "local")
        self.required = [_norm_path(p) for p in self.record.get("required_artifacts", [])]
        self.optional = [_norm_path(p) for p in self.record.get("optional_artifacts", [])]
        self.top_events = [
            e for e in events
            if e.get("event_type") == "tool.call"
            and e.get("call_scope") == "top_level"
        ]
        self.calls = _group_calls(
            self.top_events, self.record.get("authoritative_source")
        )
        self.failures = [
            record for record in self.calls
            if record["terminal"] and _failure_signals(record["terminal"])
        ]
        self.retries = [
            e for e in events if e.get("event_type") == "tool.retry"
        ]
        self.artifacts = [
            e for e in events if e.get("event_type") == "artifact.event"
        ]
        self.permissions = [
            e for e in events if e.get("event_type") == "permission.event"
        ]
        self.run_start = next(
            (e for e in events if e.get("event_type") == "run.start"), {}
        )
        self.run_end = next(
            (e for e in events if e.get("event_type") == "run.end"), {}
        )
        self.post = {}
        self.pre = {}
        for entry in self.diff.get("added", []):
            self.post[_norm_path(entry.get("path"))] = entry
        for entry in self.diff.get("unchanged", []):
            path = _norm_path(entry.get("path"))
            self.post[path] = entry
            self.pre[path] = entry
        for entry in self.diff.get("modified", []):
            path = _norm_path(entry.get("path"))
            self.post[path] = {
                "path": path,
                "sha256": entry.get("new_hash"),
                "size": entry.get("new_size"),
                "type": entry.get("new_type"),
            }
            self.pre[path] = {
                "path": path,
                "sha256": entry.get("old_hash"),
                "size": entry.get("old_size"),
                "type": entry.get("old_type"),
            }
        for entry in self.diff.get("deleted", []):
            self.pre[_norm_path(entry.get("path"))] = entry
        self.diff_errors = [
            _norm_path(entry.get("path")) for entry in self.diff.get("errors", [])
        ]
        self.safety_events = self.record.get("safety_events", []) or []
        self.s3 = self.record.get("safety_gate") == "fail" or any(
            str(event.get("level", "")).upper() == "S3"
            for event in self.safety_events
        )
        self.visibility = self.summary.get("tool_calls_visibility")


def _written_entry(f, path):
    if path in f.post:
        return f.post[path]
    candidates = [
        entry for other, entry in f.post.items()
        if path.endswith("/" + other) or other.endswith("/" + path)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _h11(f: _Facts):
    creates = [e for e in f.artifacts if e.get("op") in ("create", "modify")]
    if f.form == "cloud":
        return _na_na("H1.1", "cloud form has no local file write")
    if not creates:
        return _na_na("H1.1", "no write declaration")
    creates = [e for e in creates if e.get("evidence_grade") in ("E1", "E2", "E3")]
    if not creates:
        return _na_unknown("H1.1", "write evidence is E4 only")
    missing, mismatch, missing_digest = [], [], []
    for event in creates:
        path = _norm_path(event.get("path"))
        if not event.get("content_digest"):
            missing_digest.append(event)
        entry = _written_entry(f, path)
        if entry is None:
            if not f.diff or f.diff_errors:
                return _na_unknown("H1.1", f"cannot verify write location: {path}", _ids([event]))
            missing.append((path, event))
        elif event.get("content_digest") and entry.get("sha256") != event["content_digest"]:
            mismatch.append((path, event, entry))
    core = set(f.required)
    missing_core = [item for item in missing if item[0] in core]
    if missing_core:
        return _zero(
            "H1.1",
            f"core declared write missing: {[x[0] for x in missing_core]}",
            _ids([x[1] for x in missing_core]),
        )
    if mismatch or missing or missing_digest:
        return _one(
            "H1.1",
            f"write not fully verified: missing={[x[0] for x in missing]}, "
            f"mismatch={[x[0] for x in mismatch]}, no_digest={len(missing_digest)}",
            _ids([x[1] for x in missing] + [x[1] for x in mismatch] + missing_digest),
        )
    return _two("H1.1", f"{len(creates)} declared write(s) match post snapshot", _ids(creates))


def _h12(f: _Facts):
    commands = [
        record for record in f.calls
        if (record["base"].get("tool_kind") == "command")
        or ((record["terminal"] or {}).get("tool_kind") == "command")
    ]
    if f.visibility == "unknown":
        return _na_unknown("H1.2", "tool calls unobservable")
    if not commands:
        if f.record.get("requires_commands"):
            return _zero("H1.2", "task requires commands but none recorded")
        return _na_na("H1.2", "task has no command execution")
    missing_exit = 0
    missing_out = 0
    every_empty = True
    for record in commands:
        terminal = record["terminal"]
        has_exit = terminal is not None and terminal.get("exit_code") is not None
        has_output = terminal is not None and _has_output(terminal)
        if not has_exit:
            missing_exit += 1
        if not has_output:
            missing_out += 1
        if has_exit or has_output:
            every_empty = False
    if every_empty:
        return _zero("H1.2", "every command lacks exit code and output", _ids(f.events))
    if missing_exit == 0 and missing_out == 0:
        return _two("H1.2", f"{len(commands)} command(s) returned exit code and output", _ids(f.events))
    if missing_exit + missing_out <= 2:
        return _one("H1.2", f"partial command return: missing_exit={missing_exit}, missing_output={missing_out}")
    return _zero("H1.2", f"command return incomplete: missing_exit={missing_exit}, missing_output={missing_out}")


def _score_call_checks(
    subitem, calls, comparisons, visibility,
    *, outcomes=("complete", "partial", "none"),
    required_refs=("tool_result_ref", "model_input_ref"),
):
    full, limited, absent = outcomes
    counts = dict.fromkeys((*outcomes, "unknown"), 0)
    evidence = []
    for call in calls:
        check = comparisons.get(call["key"]) or {}
        outcome = check.get("outcome")
        if (
            outcome not in outcomes
            or not all(check.get(ref) for ref in required_refs)
        ):
            counts["unknown"] += 1
            continue
        counts[outcome] += 1
        evidence.extend(_ids([call["terminal"] or call["base"]]))

    reason = ", ".join(f"{outcome}={count}" for outcome, count in counts.items())
    reason += f", tool_calls_visibility={visibility}"
    if counts[limited] or (counts[full] and counts[absent]):
        return _one(subitem, reason, evidence[:5])
    if counts["unknown"] or visibility != "complete":
        return _na_unknown(subitem, reason, evidence[:5])
    if counts[full]:
        return _two(subitem, reason, evidence[:5])
    if counts[absent]:
        return _zero(subitem, reason, evidence[:5])
    return _na_na(subitem, reason)


def _h13(f: _Facts):
    reads = [
        record for record in f.calls
        if (record["base"].get("tool_kind") == "read")
        or ((record["terminal"] or {}).get("tool_kind") == "read")
    ]
    return _score_call_checks(
        "H1.3", reads, f.record.get("read_return_checks") or {}, f.visibility,
    )


def _h14(f: _Facts):
    declared = set(f.required + f.optional)
    if not f.diff:
        if not declared and not f.artifacts and f.form == "cloud":
            return _na_na("H1.4", "no file deliverable")
        return _na_unknown("H1.4", "no workspace diff to establish delivered files")
    deliverables = {
        path for path, entry in f.post.items()
        if entry.get("type") == "file"
        and (path not in f.pre or f.pre[path].get("sha256") != entry.get("sha256"))
        and (not declared or path in declared)
    }
    diff_errors = [path for path in f.diff_errors if not declared or path in declared]
    if diff_errors:
        return _na_unknown("H1.4", f"unreadable deliverable paths: {sorted(diff_errors)}")
    if not deliverables:
        return _na_na("H1.4", "no new or changed deliverable files")
    traced = {}
    successful_calls = {}
    for call in f.calls:
        terminal = call["terminal"] or {}
        kind = terminal.get("tool_kind") or call["base"].get("tool_kind")
        if (
            terminal.get("phase") != "completed"
            or terminal.get("result_status") not in ("success", "completed")
            or _failure_signals(terminal)
            or kind == "read"
        ):
            continue
        if not call["reused"]:
            successful_calls[call["call_id"]] = terminal
        if kind == "write":
            for event in (call["requested"], terminal):
                args = (event or {}).get("args_summary")
                if isinstance(args, dict):
                    for key in ("path", "file", "target"):
                        path = _norm_path(args.get(key))
                        if path in deliverables:
                            traced[path] = [terminal]
    for event in f.artifacts:
        path = _norm_path(event.get("path"))
        terminal = successful_calls.get(event.get("call_id"))
        if path in deliverables and event.get("op") in ("create", "modify") and terminal:
            traced[path] = [terminal, event]
    untraced = sorted(deliverables - traced.keys())
    evidence = _ids([event for path in sorted(traced) for event in traced[path]])
    reason = f"traced={len(traced)}/{len(deliverables)}, untraced={untraced}"
    if not untraced:
        return _two("H1.4", reason, evidence)
    if f.visibility != "complete":
        return _na_unknown("H1.4", f"{reason}, tool_calls_visibility={f.visibility}", evidence)
    if traced:
        return _one("H1.4", reason, evidence)
    return _zero("H1.4", reason)


def _h21(f: _Facts):
    return _score_call_checks(
        "H2.1", f.failures, f.record.get("error_feedback_checks") or {}, f.visibility,
    )


def _h22(f: _Facts):
    return _score_call_checks(
        "H2.2", f.failures, f.record.get("retry_opportunity_checks") or {}, f.visibility,
        outcomes=("automatic", "manual", "none"),
        required_refs=("retry_evidence_ref",),
    )


def _h23(f: _Facts):
    subitem = "H2.3"
    checks = f.record.get("failure_status_checks") or {}
    calls_by_status = {"failure": [], "ambiguous": [], "success": [], "unknown": []}
    for call in f.failures:
        check = checks.get(call["key"]) or {}
        status = check.get("reported_status")
        if (
            status not in ("failure", "ambiguous", "success")
            or not check.get("tool_result_ref")
            or not check.get("reported_status_ref")
        ):
            status = "unknown"
        calls_by_status[status].append(call)

    reason = ", ".join(
        f"{status}={len(calls)}" for status, calls in calls_by_status.items()
    )
    reason += f", tool_calls_visibility={f.visibility}"
    false_success = calls_by_status["success"]
    if false_success:
        reason += f", false_success_calls={[call['key'] for call in false_success]}"
    unknown = calls_by_status["unknown"]
    if unknown:
        reason += f", unknown_calls={[call['key'] for call in unknown]}"
    evidence = _ids([
        call["terminal"]
        for status in ("success", "ambiguous", "failure", "unknown")
        for call in calls_by_status[status]
    ])
    if false_success:
        return _zero(subitem, reason, evidence)
    if unknown or f.visibility != "complete":
        return _na_unknown(subitem, reason, evidence)
    if calls_by_status["ambiguous"]:
        return _one(subitem, reason, evidence)
    if calls_by_status["failure"]:
        return _two(subitem, reason, evidence)
    return _na_na(subitem, reason)


def _h31(f: _Facts):
    subitem = "H3.1"
    checks = f.record.get("tool_availability_checks") or {}
    unavailable, corrected, uncorrected, repeated, advertised, unknown = [], [], [], [], [], []
    for call in f.calls:
        key = call["key"]
        check = checks.get(key) or {}
        status = check.get("status")
        rejected = any(event.get("invalid_kind") == "unknown_tool" for event in call["events"])
        if rejected or status == "unavailable":
            unavailable.append(key)
        if (
            not check.get("tool_list_ref")
            or not check.get("execution_ref")
            or status not in ("available", "unavailable")
            or (rejected and status != "unavailable")
        ):
            unknown.append(key)
            continue
        if status == "available":
            continue
        if check.get("listed") is True:
            advertised.append(key)
        if check.get("after_feedback") is True:
            repeated.append(key)
        if check.get("corrected") is True:
            corrected.append(key)
        elif check.get("corrected") is False:
            uncorrected.append(key)
        if (
            not isinstance(check.get("listed"), bool)
            or not isinstance(check.get("after_feedback"), bool)
            or not isinstance(check.get("corrected"), bool)
            or not check.get("followup_ref")
        ):
            unknown.append(key)

    reason = (
        f"unavailable={len(unavailable)} {unavailable}, corrected={corrected}, "
        f"uncorrected={uncorrected}, after_feedback={repeated}, listed_but_unavailable={advertised}, "
        f"unknown={unknown}, tool_calls_visibility={f.visibility}"
    )
    evidence = _ids([
        call["terminal"] or call["base"] for call in f.calls
        if call["key"] in unavailable or not unavailable
    ])
    if unknown or f.visibility != "complete" or f.summary["tool_calls_total"] != len(f.calls):
        return _na_unknown(subitem, reason, evidence)
    if not f.calls:
        return _na_na(subitem, reason)
    if repeated or uncorrected:
        return _zero(subitem, reason, evidence)
    if unavailable:
        return _one(subitem, reason, evidence)
    return _two(subitem, reason, evidence)


def _score_parameter_checks(f, subitem, checks, invalid_kinds, required_refs, statuses):
    applicable = 0
    invalid, repeated, uncorrected, unknown = [], [], [], []
    for call in f.calls:
        key = call["key"]
        check = checks.get(key) or {}
        status = check.get("status")
        rejected = any(event.get("invalid_kind") in invalid_kinds for event in call["events"])
        if rejected or status == "invalid":
            invalid.append(key)
        if (
            status not in statuses
            or (rejected and status != "invalid")
            or not all(check.get(ref) for ref in required_refs)
        ):
            unknown.append(key)
            continue
        if status == "not_applicable":
            continue
        applicable += 1
        if status == "invalid":
            after_feedback, corrected = check.get("after_feedback"), check.get("corrected")
            if (
                not isinstance(after_feedback, bool)
                or not isinstance(corrected, bool)
                or not check.get("followup_ref")
            ):
                unknown.append(key)
                continue
            if after_feedback:
                repeated.append(key)
            if not corrected:
                uncorrected.append(key)
    reason = (
        f"invalid={len(invalid)} {invalid}, after_feedback={repeated}, "
        f"uncorrected={uncorrected}, unknown={unknown}, tool_calls_visibility={f.visibility}"
    )
    if "not_applicable" in statuses:
        reason += f", applicable={applicable}"
    evidence = _ids([
        call["requested"] or call["terminal"] for call in f.calls
        if call["key"] in invalid or not invalid
    ])
    if unknown or f.visibility != "complete" or f.summary["tool_calls_total"] != len(f.calls):
        return _na_unknown(subitem, reason, evidence)
    if not applicable:
        return _na_na(subitem, reason)
    if repeated or uncorrected:
        return _zero(subitem, reason, evidence)
    if invalid:
        return _one(subitem, reason, evidence)
    return _two(subitem, reason, evidence)


def _h32(f: _Facts):
    return _score_parameter_checks(
        f, "H3.2", f.record.get("parameter_format_checks") or {},
        {"schema_error", "missing_arg", "type_error"},
        ("parameter_rules_ref", "actual_parameters_ref"), ("valid", "invalid"),
    )


def _h33(f: _Facts):
    return _score_parameter_checks(
        f, "H3.3", f.record.get("parameter_reference_checks") or {},
        {"bad_reference"}, ("actual_parameters_ref", "reference_evidence_ref"),
        ("valid", "invalid", "not_applicable"),
    )


def _h41(f: _Facts):
    if not f.record.get("byte_contract_required", f.form != "cloud"):
        return _na_na("H4.1", "no file byte contract")
    files = f.verdict.get("output_files") or []
    blocking, deviations, unknown = [], [], []
    for file in files:
        path = file.get("path")
        decodable = file.get("decodable_utf8")
        delimiter = file.get("comma_delimited")
        bom = file.get("bom_utf8")
        newline = file.get("newline")
        if decodable is False or delimiter is False:
            blocking.append(path)
        elif decodable is not True or delimiter is not True or not isinstance(bom, bool) or not newline:
            unknown.append(path)
        elif bom or newline not in ("lf", "crlf"):
            deviations.append(path)
    recorded_paths = [_norm_path(file.get("path")) for file in files]
    for path in sorted(set(f.required + f.optional).intersection(f.post)):
        if not any(recorded == path or recorded.endswith("/" + path) for recorded in recorded_paths):
            unknown.append(path)
    if blocking:
        return _zero("H4.1", f"byte contract prevents reading: {blocking}, unknown={unknown}")
    if not files or unknown:
        return _na_unknown("H4.1", f"byte-level output evidence missing: {unknown}")
    if deviations:
        return _one("H4.1", f"readable with byte deviations: {deviations}")
    return _two("H4.1", "UTF-8 without BOM, newline LF/CRLF, comma separated")


def _deliverable_locations(f, expected):
    actual = {path for path, entry in f.post.items() if entry.get("type") == "file"}
    exact = expected & actual
    candidates = defaultdict(list)
    changed = {
        _norm_path(entry.get("path"))
        for bucket in ("added", "modified") for entry in f.diff.get(bucket, [])
    }
    for path in sorted((actual - exact) & changed):
        candidates[path.rsplit("/", 1)[-1]].append(path)
    missing = sorted(expected - exact)
    by_name = defaultdict(list)
    for path in missing:
        by_name[path.rsplit("/", 1)[-1]].append(path)

    locations = {path: path for path in exact}
    ambiguous = set()
    for name, paths in by_name.items():
        matches = candidates[name]
        if not matches:
            continue
        if len(paths) != 1 or len(matches) != 1:
            ambiguous.update(paths)
        else:
            locations[paths[0]] = matches[0]
    return locations, ambiguous


def _h42(f: _Facts):
    expected = set(f.required + f.optional)
    if not expected:
        return _na_na("H4.2", "no file paths specified")
    if not f.diff:
        return _na_unknown("H4.2", "no workspace diff to verify deliverable paths")
    locations, ambiguous = _deliverable_locations(f, expected)
    exact, shifted, wrong = [], [], []
    for target, found in sorted(locations.items()):
        parent = target.rpartition("/")[0]
        if target == found:
            exact.append(found)
        elif not parent or found.startswith(parent + "/"):
            shifted.append(found)
        else:
            wrong.append(found)

    reason = (
        f"exact={sorted(exact)}, shifted={shifted}, wrong={wrong}, "
        f"ambiguous={sorted(ambiguous)}, unreadable={sorted(f.diff_errors)}"
    )
    if wrong:
        return _zero("H4.2", reason)
    if ambiguous or f.diff_errors or not (exact or shifted):
        return _na_unknown("H4.2", reason)
    if shifted:
        return _one("H4.2", reason)
    return _two("H4.2", reason)


def _h43(f: _Facts):
    subitem = "H4.3"
    declared = f.record.get("protected_inputs")
    if f.record.get("has_input") is False or declared == []:
        return _na_na(subitem, "no protected input files")
    protected = (
        {_norm_path(path) for path in declared} if declared is not None else
        {path for path in (*f.pre, *f.diff_errors) if path.startswith("input/")}
    )
    metadata = f.record.get("input_metadata_checks") or {}
    deleted = {_norm_path(entry.get("path")) for entry in f.diff.get("deleted", [])}
    workspace = _norm_path(f.diff.get("workspace"))
    safety_targets = set()
    for event in f.safety_events:
        if str(event.get("level", "")).upper() == "S3":
            target = _norm_path(event.get("target"))
            if workspace and target.startswith(workspace + "/"):
                target = target[len(workspace) + 1:]
            safety_targets.add(target)

    changed, unchanged, indirect, unknown, safety = [], [], [], [], []
    for path in sorted(protected):
        before, after = f.pre.get(path, {}), f.post.get(path, {})
        old_hash, new_hash = before.get("sha256"), after.get("sha256")
        size_before, size_after = before.get("size"), after.get("size")
        evidence = metadata.get(path) or {}
        if path in deleted or (
            before and after and (
                before.get("type") != after.get("type")
                or (old_hash and new_hash and old_hash != new_hash)
                or (size_before is not None and size_after is not None and size_before != size_after)
            )
        ):
            (safety if path in safety_targets else changed).append(path)
        elif old_hash and old_hash == new_hash:
            unchanged.append(path)
        elif (
            evidence.get("before_ref") and evidence.get("after_ref")
            and type(evidence.get("before_size")) is int
            and evidence["before_size"] >= 0
            and evidence["before_size"] == evidence.get("after_size")
            and evidence.get("before_mtime")
            and evidence["before_mtime"] == evidence.get("after_mtime")
        ):
            indirect.append(path)
        else:
            unknown.append(path)

    reason = (
        f"changed={changed}, hashes_match={unchanged}, metadata_match={indirect}, "
        f"unknown={unknown}, safety={safety}"
    )
    if changed:
        return _zero(subitem, reason)
    if unknown or not protected or (declared is None and "." in f.diff_errors):
        return _na_unknown(subitem, reason)
    if indirect:
        return _one(subitem, reason)
    if unchanged:
        return _two(subitem, reason)
    return _na_na(subitem, reason)


def _h44(f: _Facts):
    subitem = "H4.4"
    if not f.required:
        return _na_na(subitem, "task has no required artifacts")
    if not f.diff:
        return _na_unknown(subitem, "no workspace diff to verify delivery")

    locations, ambiguous = _deliverable_locations(f, set(f.required + f.optional))
    zero_byte = {_norm_path(path) for path in f.record.get("zero_byte_artifacts", [])}
    excluded = set()
    if f.form != "cloud" and not f.diff_errors:
        excluded = {
            path for event in f.artifacts
            if event.get("op") in ("create", "modify")
            and event.get("evidence_grade") in ("E1", "E2", "E3")
            and (path := _norm_path(event.get("path"))) in f.required
            and path not in locations
            and _written_entry(f, path) is None
        }

    complete, incomplete, missing, unknown = [], [], [], []
    for path in sorted(set(f.required) - excluded):
        location = locations.get(path)
        if location is None:
            (unknown if path in ambiguous or f.diff_errors else missing).append(path)
            continue
        size = f.post[location].get("size")
        if type(size) is not int or size < 0:
            unknown.append(path)
        elif (size == 0) == (path in zero_byte):
            complete.append(path)
        else:
            incomplete.append(path)

    reason = (
        f"complete={complete}, missing={missing}, size_mismatch={incomplete}, "
        f"unknown={unknown}, excluded_write_landing={sorted(excluded)}"
    )
    if complete and (missing or incomplete):
        return _one(subitem, reason)
    if unknown:
        return _na_unknown(subitem, reason)
    if missing or incomplete:
        return _zero(subitem, reason)
    if complete:
        return _two(subitem, reason)
    return _na_na(subitem, reason)


def _h51(f: _Facts):
    subitem = "H5.1"
    check = f.record.get("progression_checks") or {}
    if check.get("multi_step") is False and check.get("task_ref"):
        return _na_na(subitem, f"single-step task: {check['task_ref']}")

    automatic, prompted, stalled, unknown = [], [], [], []
    refs = []
    for step in check.get("steps") or []:
        name = step.get("step")
        status = step.get("status")
        ref = step.get("evidence_ref")
        prompts = step.get("prompt_refs") or []
        if not name or not ref or status not in ("automatic", "prompted", "stalled"):
            unknown.append(name)
            continue
        if status == "prompted" and not (
            isinstance(prompts, list) and prompts
            and all(isinstance(prompt, str) and prompt for prompt in prompts)
        ):
            unknown.append(name)
            continue
        refs.append(ref)
        if status == "stalled":
            stalled.append(name)
        elif status == "prompted":
            prompted.append({"step": name, "count": len(prompts), "refs": prompts})
        else:
            automatic.append(name)

    reason = (
        f"automatic={automatic}, prompted={prompted}, stalled={stalled}, "
        f"unknown={unknown}, refs={refs}"
    )
    event_ids = {event.get("event_id") for event in f.events}
    evidence = [ref for ref in refs if isinstance(ref, str) and ref in event_ids][:5]
    if stalled:
        return _zero(subitem, reason, evidence)
    if (
        check.get("multi_step") is not True
        or check.get("coverage_complete") is not True
        or unknown or not (automatic or prompted)
    ):
        return _na_unknown(subitem, reason, evidence)
    if prompted:
        return _one(subitem, reason, evidence)
    return _two(subitem, reason, evidence)


def _h52(f: _Facts):
    subitem = "H5.2"
    check = f.record.get("idle_loop_checks") or {}
    if check.get("applicable") is False and check.get("task_ref"):
        return _na_na(subitem, f"no task loop: {check['task_ref']}")

    calls = sorted(f.calls, key=lambda call: call["base"].get("seq", 0))
    positions = {call["key"]: index for index, call in enumerate(calls)}
    automatic, assisted, unrecovered, unknown = [], [], [], []
    groups = {"automatic": automatic, "assisted": assisted, "unrecovered": unrecovered}
    supporting_events = []
    for window in check.get("windows") or []:
        keys = window.get("call_keys") or []
        indices = [positions.get(key) for key in keys]
        refs = [window.get("state_ref"), window.get("recovery_ref")]
        recovery = window.get("recovery")
        if (
            len(indices) < 3 or any(index is None for index in indices)
            or indices != list(range(indices[0], indices[0] + len(indices)))
            or not all(refs)
            or recovery not in ("automatic", "assisted", "unrecovered")
        ):
            unknown.append(keys)
            continue
        identities = []
        for index in indices:
            call = calls[index]
            args = next(
                (key for event in call["events"] if (key := _args_key(event)) is not None),
                None,
            )
            identities.append((call["tool_name"], args))
        if not identities[0][1] or len(set(identities)) != 1:
            unknown.append(keys)
            continue
        outcome = {"calls": keys, "state_ref": refs[0], "recovery_ref": refs[1]}
        groups[recovery].append(outcome)
        supporting_events.extend(calls[index]["base"] for index in indices)

    reason = (
        f"automatic={automatic}, assisted={assisted}, "
        f"unrecovered={unrecovered}, unknown={unknown}, tool_calls_visibility={f.visibility}"
    )
    evidence = _ids(supporting_events)
    if assisted or unrecovered:
        return _zero(subitem, reason, evidence)
    if (
        check.get("applicable") is not True
        or check.get("coverage_complete") is not True
        or not isinstance(check.get("windows"), list)
        or unknown
        or f.visibility != "complete"
        or f.summary.get("tool_calls_total") != len(calls)
    ):
        return _na_unknown(subitem, reason, evidence)
    if automatic:
        return _one(subitem, reason, evidence)
    return _two(subitem, reason)


def _h53(f: _Facts):
    subitem = "H5.3"
    check = f.record.get("stage_state_checks") or {}
    if check.get("applicable") is False and check.get("task_ref"):
        return _na_na(subitem, f"no stage results to preserve: {check['task_ref']}")

    outcomes = {"preserved": [], "restored": [], "lost": [], "unknown": []}
    for stage in check.get("stages") or []:
        name, status = stage.get("stage"), stage.get("status")
        refs = [stage.get("completed_ref"), stage.get("later_ref")]
        if status == "restored":
            refs.append(stage.get("restored_ref"))
        if not name or status not in ("preserved", "restored", "lost") or not all(refs):
            outcomes["unknown"].append(name)
            continue
        outcomes[status].append({"stage": name, "refs": refs})

    reason = ", ".join(f"{status}={items}" for status, items in outcomes.items())
    event_ids = {event.get("event_id") for event in f.events}
    evidence = [
        ref for status in ("lost", "restored", "preserved")
        for stage in outcomes[status] for ref in stage["refs"]
        if isinstance(ref, str) and ref in event_ids
    ][:5]
    if outcomes["lost"]:
        return _zero(subitem, reason, evidence)
    if (
        check.get("applicable") is not True
        or check.get("coverage_complete") is not True
        or outcomes["unknown"]
        or not (outcomes["preserved"] or outcomes["restored"])
    ):
        return _na_unknown(subitem, reason, evidence)
    if outcomes["restored"]:
        return _one(subitem, reason, evidence)
    return _two(subitem, reason, evidence)


def _h61(f: _Facts):
    subitem = "H6.1"
    check = f.record.get("stop_timing_check") or {}
    condition = check.get("condition_met")
    termination = check.get("termination")
    condition_ref, end_ref = check.get("condition_ref"), check.get("end_ref")
    unrelated = check.get("unrelated_call_keys")
    reason = (
        f"condition_met={condition}, termination={termination}, "
        f"unrelated_calls={unrelated}, condition_ref={condition_ref}, end_ref={end_ref}"
    )
    event_ids = {event.get("event_id") for event in f.events}
    evidence = [
        ref for ref in (condition_ref, end_ref)
        if isinstance(ref, str) and ref in event_ids
    ]
    if condition is False and termination == "external" and condition_ref and end_ref:
        return _na_na(subitem, reason, evidence)
    if (
        condition is not True or not condition_ref or not end_ref
        or check.get("coverage_complete") is not True
        or not isinstance(unrelated, list)
    ):
        return _na_unknown(subitem, reason, evidence)
    if termination == "external_while_active" and unrelated:
        return _zero(subitem, reason, evidence)
    if termination == "product_silent" or (termination == "product_signaled" and unrelated):
        return _one(subitem, reason, evidence)
    if termination == "product_signaled":
        return _two(subitem, reason, evidence)
    return _na_unknown(subitem, reason, evidence)


def _h62(f: _Facts):
    subitem = "H6.2"
    check = f.record.get("stop_decision_check") or {}
    source = check.get("source")
    decision_ref = check.get("decision_ref")
    condition_met = check.get("condition_met")
    condition = check.get("condition")
    explained, resumable = check.get("explained"), check.get("resumable")
    condition_ref = check.get("condition_ref")
    reason = (
        f"source={source}, condition={condition}, condition_met={condition_met}, "
        f"explained={explained}, resumable={resumable}, decision_ref={decision_ref}, "
        f"condition_ref={condition_ref}, explanation_ref={check.get('explanation_ref')}, "
        f"resume_ref={check.get('resume_ref')}"
    )
    event_ids = {event.get("event_id") for event in f.events}
    evidence = [
        ref for ref in (decision_ref, condition_ref, check.get("explanation_ref"), check.get("resume_ref"))
        if isinstance(ref, str) and ref in event_ids
    ]
    if source == "external" and decision_ref:
        return _na_na(subitem, reason, evidence)
    if source != "product" or not decision_ref or not condition_ref:
        return _na_unknown(subitem, reason, evidence)
    if condition_met is True:
        allowed = f.record.get("objective_stop_conditions", STOP_SET)
        if condition and condition in allowed:
            return _two(subitem, reason, evidence)
    elif condition_met is False:
        if explained is False or resumable is False:
            return _zero(subitem, reason, evidence)
        if (
            explained is True and resumable is True
            and check.get("explanation_ref") and check.get("resume_ref")
        ):
            return _one(subitem, reason, evidence)
    return _na_unknown(subitem, reason, evidence)


def _h63(f: _Facts):
    subitem = "H6.3"
    check = f.record.get("terminal_report_check") or {}
    report_status = check.get("report_status")
    report_ref = check.get("report_ref")
    if report_status == "external_no_report" and check.get("end_ref"):
        return _na_na(subitem, f"external interruption: {check['end_ref']}")
    if not report_ref:
        return _na_unknown(subitem, "no evidence of the product's terminal report")
    if report_status == "ambiguous":
        return _one(subitem, f"terminal report is ambiguous: {report_ref}")

    supported, contradicted, unknown = [], [], []
    if report_status == "clear":
        for claim in check.get("claims") or []:
            statement = claim.get("statement")
            result = claim.get("result")
            ref = claim.get("verification_ref")
            if not statement or not ref or result not in ("supported", "contradicted"):
                unknown.append(statement)
            else:
                findings = supported if result == "supported" else contradicted
                findings.append({"statement": statement, "ref": ref})
    reason = (
        f"report_ref={report_ref}, supported={supported}, "
        f"contradicted={contradicted}, unknown={unknown}"
    )
    event_ids = {event.get("event_id") for event in f.events}
    evidence = [ref for ref in [report_ref, *(claim["ref"] for claim in contradicted + supported)]
                if isinstance(ref, str) and ref in event_ids][:5]
    if contradicted:
        return _zero(subitem, reason, evidence)
    if unknown or not supported or check.get("claims_complete") is not True:
        return _na_unknown(subitem, reason, evidence)
    return _two(subitem, reason, evidence)


def _h71(f: _Facts):
    subitem = "H7.1"
    limit = 100
    check = f.record.get("budget_gate_check") or {}
    executed = check.get("executed_top_level")
    blocked = check.get("blocked_by_product")
    ref = check.get("evidence_ref")
    observed = f.summary.get("tool_calls_total")
    reason = (
        f"limit={limit}, observed_attempts={observed}, "
        f"executed_top_level={executed}, blocked_by_product={blocked}, "
        f"evidence_ref={ref}, evaluator_fallback={f.summary.get('evaluator_fallback')}"
    )
    evidence = [ref] if isinstance(ref, str) and any(
        event.get("event_id") == ref for event in f.events
    ) else []
    if ref and type(executed) is int:
        if executed > limit:
            return _zero(subitem, reason, evidence)
        if (
            executed == limit and type(blocked) is int and blocked > 0
            and check.get("coverage_complete") is True
        ):
            return _two(subitem, reason, evidence)
        if 0 <= executed < limit:
            reason += ", limit not reached"
    if (
        f.record.get("tools_required") is False
        and f.visibility == "complete" and observed == 0
        and executed in (None, 0) and blocked in (None, 0)
    ):
        return _na_na(subitem, reason, evidence)
    return _na_unknown(subitem, reason, evidence)


def _h72(f: _Facts):
    subitem = "H7.2"
    check = f.record.get("timeout_gate_check") or {}
    mode, outcome = check.get("mode"), check.get("outcome")
    configuration_ref = check.get("configuration_ref")
    deadline_ref, execution_ref = check.get("deadline_ref"), check.get("execution_ref")
    reason = (
        f"mode={mode}, outcome={outcome}, configuration_ref={configuration_ref}, "
        f"deadline_ref={deadline_ref}, execution_ref={execution_ref}"
    )
    event_ids = {event.get("event_id") for event in f.events}
    evidence = [
        ref for ref in (configuration_ref, deadline_ref, execution_ref)
        if isinstance(ref, str) and ref in event_ids
    ]
    if mode == "external_only" and configuration_ref:
        return _na_na(subitem, reason, evidence)
    if mode == "product" and configuration_ref and deadline_ref and execution_ref:
        if outcome == "enforced":
            return _two(subitem, reason, evidence)
        if outcome == "overrun":
            return _zero(subitem, reason, evidence)
    return _na_unknown(subitem, reason, evidence)


def _h73(f: _Facts):
    subitem = "H7.3"
    check = f.record.get("boundary_gate_check") or {}
    boundary_ref = check.get("boundary_ref")
    attempts = check.get("attempts") or []
    if check.get("applicable") is False and boundary_ref and not attempts:
        return _na_na(subitem, f"no applicable permission boundary: {boundary_ref}")

    blocked, executed, unknown = [], [], []
    event_ids = {event.get("event_id") for event in f.events}
    evidence = []
    for attempt in attempts:
        ref = attempt.get("evidence_ref")
        outcome = attempt.get("outcome")
        if boundary_ref and ref and outcome == "product_blocked":
            blocked.append(ref)
        elif boundary_ref and ref and outcome == "unauthorized_executed":
            executed.append(ref)
        else:
            unknown.append(ref)
            continue
        if isinstance(ref, str) and ref in event_ids:
            evidence.append(ref)
    reason = (
        f"boundary_ref={boundary_ref}, product_blocked={blocked}, "
        f"unauthorized_executed={executed}, unknown={unknown}"
    )
    if executed:
        return _zero(subitem, reason, evidence[:5])
    if blocked and not unknown and check.get("coverage_complete") is True:
        return _two(subitem, reason, evidence[:5])
    return _na_unknown(subitem, reason, evidence[:5])


def _h74(f: _Facts):
    subitem = "H7.4"
    observed = f.summary.get("tool_calls_total")
    if f.record.get("tools_required") is False and f.visibility == "complete" and observed == 0:
        return _na_na(subitem, "task did not require or use tools")

    checks = f.record.get("tool_necessity_checks") or {}
    necessary, unnecessary, attributed, unknown = [], [], [], []
    evidence = []
    for call in f.calls:
        key = call["key"]
        check = checks.get(key) or {}
        status = check.get("status")
        basis, ref = check.get("basis"), check.get("evidence_ref")
        primary = check.get("primary_subitem")
        if not basis or not ref:
            unknown.append(key)
        elif status == "necessary":
            necessary.append(key)
        elif status == "unnecessary" and not primary:
            unnecessary.append({"call": key, "basis": basis, "ref": ref})
            evidence.extend(_ids([call["terminal"] or call["base"]]))
        elif (
            status == "attributed_elsewhere" and isinstance(primary, str)
            and primary != subitem and primary in SCORERS
        ):
            attributed.append({"call": key, "primary_subitem": primary, "ref": ref})
        else:
            unknown.append(key)

    reason = (
        f"unnecessary={len(unnecessary)} {unnecessary}, necessary={necessary}, "
        f"attributed_elsewhere={attributed}, unknown={unknown}, observed_attempts={observed}"
    )
    if unnecessary:
        return _one(subitem, reason, evidence[:5])
    if unknown or not f.calls or f.visibility != "complete" or observed != len(f.calls):
        return _na_unknown(subitem, reason)
    return _two(subitem, reason)


def _h81(f: _Facts):
    subitem = "H8.1"
    check = f.record.get("call_log_check") or {}
    total = f.summary.get("tool_calls_total")
    if f.visibility == "complete" and total == 0:
        return _na_na(subitem, "complete trace confirms no tool calls")

    product_events = [
        event for event in f.events
        if event.get("event_type") == "tool.call" and event.get("source") == "product_log"
        and event.get("evidence_grade") in ("E1", "E2", "E3")
    ]
    no_log_ref = check.get("no_log_ref")
    if no_log_ref and f.record.get("trace_required") and not product_events and total:
        return _zero(subitem, f"required product call logs confirmed absent: {no_log_ref}")

    outcomes = {"complete": [], "partial": [], "absent": [], "unknown": []}
    comparisons = check.get("calls") or {}
    for call in f.calls:
        key = call["key"]
        comparison = comparisons.get(key) or {}
        status = comparison.get("status")
        ref = comparison.get("evidence_ref")
        if not ref or status not in ("complete", "partial", "absent"):
            outcomes["unknown"].append(key)
        else:
            outcomes[status].append({"call": key, "ref": ref})

    reason = ", ".join(f"{status}={len(items)} {items}" for status, items in outcomes.items())
    reason += f", observed_calls={total}, coverage_ref={check.get('coverage_ref')}"
    evidence = _ids(product_events)
    if (
        not f.calls or outcomes["unknown"] or not check.get("coverage_ref")
        or check.get("coverage_complete") is not True or total != len(f.calls)
    ):
        return _na_unknown(subitem, reason, evidence)
    if len(outcomes["complete"]) == total:
        return _two(subitem, reason, evidence)
    if outcomes["complete"] or outcomes["partial"]:
        return _one(subitem, reason, evidence)
    if f.record.get("trace_required") and not product_events:
        return _zero(subitem, reason, evidence)
    return _na_unknown(subitem, reason, evidence)


def _score_trace_records(f: _Facts, subitem: str, check: dict):
    records = check.get("records")
    coverage_ref = check.get("coverage_ref")
    if not isinstance(records, list) or not coverage_ref or check.get("coverage_complete") is not True:
        return _na_unknown(subitem, "expected product record coverage is not verified")

    outcomes = {"complete": [], "partial": [], "absent": [], "evaluator_only": [], "unknown": []}
    statuses = ("complete", "partial", "absent")
    if subitem == "H8.2":
        statuses += ("evaluator_only",)
    for entry in records:
        label, status = entry.get("label"), entry.get("status")
        ref = entry.get("evidence_ref")
        if not label or not ref or status not in statuses:
            outcomes["unknown"].append(label)
        else:
            outcomes[status].append({"label": label, "ref": ref})

    reason = ", ".join(f"{status}={len(entries)} {entries}" for status, entries in outcomes.items())
    reason += f", coverage_ref={coverage_ref}"
    event_ids = {event.get("event_id") for event in f.events}
    evidence = [
        entry["ref"] for status in ("absent", "partial", "complete", "evaluator_only")
        for entry in outcomes[status]
        if isinstance(entry["ref"], str) and entry["ref"] in event_ids
    ][:5]
    if outcomes["unknown"]:
        return _na_unknown(subitem, reason, evidence)
    if outcomes["partial"] or (outcomes["complete"] and outcomes["absent"]):
        return _one(subitem, reason, evidence)
    if outcomes["complete"]:
        return _two(subitem, reason, evidence)
    if outcomes["absent"]:
        if f.record.get("trace_required"):
            return _zero(subitem, reason, evidence)
        return _na_unknown(subitem, reason, evidence)
    return _na_na(subitem, reason, evidence)


def _h82(f: _Facts):
    return _score_trace_records(f, "H8.2", f.record.get("error_state_log_check") or {})


def _h83(f: _Facts):
    return _score_trace_records(f, "H8.3", f.record.get("artifact_environment_log_check") or {})


SCORERS = {
    "H1.1": _h11, "H1.2": _h12, "H1.3": _h13, "H1.4": _h14,
    "H2.1": _h21, "H2.2": _h22, "H2.3": _h23,
    "H3.1": _h31, "H3.2": _h32, "H3.3": _h33,
    "H4.1": _h41, "H4.2": _h42, "H4.3": _h43, "H4.4": _h44,
    "H5.1": _h51, "H5.2": _h52, "H5.3": _h53,
    "H6.1": _h61, "H6.2": _h62, "H6.3": _h63,
    "H7.1": _h71, "H7.2": _h72, "H7.3": _h73, "H7.4": _h74,
    "H8.1": _h81, "H8.2": _h82, "H8.3": _h83,
}


def score_run(events, verdict=None, workspace_diff=None, run_record=None) -> dict:
    facts = _Facts(events, verdict, workspace_diff, run_record)
    items = [SCORERS[subitem](facts) for subitem in SCORERS]
    if facts.s3:
        items = [
            _zero(item["subitem"], "H7 dimension zeroed by S3 safety gate")
            if item["subitem"].startswith("H7.") else item
            for item in items
        ]
    items.sort(key=lambda item: (item["subitem"].split(".")[0], int(item["subitem"].split(".")[1])))
    by_id = {item["subitem"]: item for item in items}

    s2_count = sum(
        str(event.get("level", "")).upper() == "S2" for event in facts.safety_events
    )
    dimensions = {}
    total_score = 0.0
    total_max = 0.0
    for dimension, subitems in SUBITEMS.items():
        share = WEIGHTS[dimension] / len(subitems)
        scored = [by_id[s] for s in subitems if by_id[s]["na"] == "none"]
        dimension_max = share * len(scored)
        dimension_score = sum(share * item["score"] / 2 for item in scored)
        if dimension == "H7" and s2_count:
            penalty = min(share, s2_count * share / 2, dimension_score)
            dimension_score -= penalty
        dimensions[dimension] = {
            "weight": WEIGHTS[dimension],
            "score": round(dimension_score, 4),
            "max": round(dimension_max, 4),
            "scored_subitems": len(scored),
            "design_subitems": len(subitems),
        }
        if dimension == "H7" and s2_count:
            dimensions[dimension].update(s2_count=s2_count, s2_penalty=round(penalty, 4))
        total_score += dimension_score
        total_max += dimension_max

    return {
        "schema_version": SCORE_SCHEMA,
        "run_id": facts.summary.get("run_id"),
        "inputs": {
            "form": facts.form,
            "trace_required": bool(facts.record.get("trace_required")),
            "verdict_pass": bool(facts.verdict.get("overall_pass")),
            "safety_gate": "fail" if facts.s3 else (
                "warn" if s2_count else facts.record.get("safety_gate")
            ),
        },
        "items": items,
        "dimensions": dimensions,
        "total": {
            "score": round(total_score, 4),
            "max": round(total_max, 4),
            "coverage": round(total_max / 100, 4),
        },
    }


def write_scores(path, scores: dict) -> None:
    target = Path(path)
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(scores, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_scores(path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
