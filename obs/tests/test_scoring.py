"""Synthetic fixtures for rubric-v1.0 scoring.

Each subitem has one positive case (expected 2) and one negative case
(expected 0). Additional N/A cases cover the unobservable paths.
No real product data is used.
"""

import json
import os
import tempfile
import unittest

from obs.cli import main as cli_main
from obs.io_jsonl import write_jsonl
from obs.parser import normalize_event
from obs.scoring import score_run

RUN_ID = "r1"


def _ev(seq, event_type, **fields):
    event = {
        "schema_version": "obs-v1",
        "event_id": f"EV-{RUN_ID}-{seq:06d}",
        "run_id": RUN_ID,
        "session_id": "s1",
        "seq": seq,
        "ts": f"2026-11-21T10:{seq // 60:02d}:{seq % 60:02d}+08:00",
        "event_type": event_type,
        "source": fields.pop("source", "product_log"),
        "observability": fields.pop("observability", "visible"),
        "actor": fields.pop("actor", "agent"),
        "evidence_grade": fields.pop("evidence_grade", "E1"),
    }
    event.update(fields)
    normalize_event(event)
    return event


def build(
    calls=(),
    artifacts=(),
    retries=(),
    permissions=(),
    user_inputs=(),
    unknowns=(),
    gaps=(),
    late_calls=(),
    start=None,
    end=None,
):
    events = []
    seq = 1

    start_fields = {
        "product": "Codex",
        "task_id": "T1",
        "log_complete": True,
        "capabilities": {},
    }
    start_fields.update(start or {})
    events.append(_ev(seq, "run.start", **start_fields))
    seq += 1

    for spec in user_inputs:
        events.append(_ev(seq, "user.input", **spec))
        seq += 1

    for spec in calls:
        call_id = spec["id"]
        base = {
            "call_id": call_id,
            "call_scope": spec.get("scope", "top_level"),
            "tool_name": spec["tool"],
            "args_digest": spec.get("args", f"sha256:{call_id}"),
        }
        if spec.get("kind"):
            base["tool_kind"] = spec["kind"]
        if spec.get("validity"):
            base["validity"] = spec["validity"]
        if spec.get("invalid_kind"):
            base["invalid_kind"] = spec["invalid_kind"]
        if spec.get("requested", True):
            events.append(_ev(seq, "tool.call", phase="requested", **base))
            seq += 1
        if spec.get("terminal", True):
            phase = spec.get("phase", "completed")
            terminal = dict(base)
            terminal["phase"] = phase
            terminal["result_status"] = spec.get(
                "result_status",
                {"completed": "success", "failed": "error", "rejected": "denied"}.get(
                    phase, "success"
                ),
            )
            for key in (
                "exit_code", "error_code", "error_message", "output_digest",
                "output_ref", "stdout_digest", "stderr_digest",
                "reject_reason", "retryable", "duration_ms",
            ):
                if key in spec:
                    terminal[key] = spec[key]
            if phase == "failed" and "error_code" not in terminal:
                terminal["error_code"] = "ERR"
            if phase == "rejected" and "reject_reason" not in terminal:
                terminal["reject_reason"] = "denied"
            events.append(_ev(seq, "tool.call", **terminal))
            seq += 1

    for spec in retries:
        events.append(_ev(
            seq, "tool.retry",
            retry_of_call_id=spec["of"],
            retry_scope=spec.get("scope", "top_level"),
            retry_index=spec.get("index", 1),
            retry_reason=spec.get("reason", "nonzero_exit"),
            result_status=spec.get("status", "success"),
            retry_outcome=spec.get("outcome", "recovered"),
            resolved_call_id=spec.get("resolved"),
        ))
        seq += 1

    for spec in artifacts:
        events.append(_ev(
            seq, "artifact.event",
            op=spec.get("op", "create"),
            path=spec["path"],
            content_digest=spec.get("digest", "sha256:file"),
            call_id=spec.get("call_id"),
            source=spec.get("source", "product_log"),
        ))
        seq += 1

    for spec in permissions:
        events.append(_ev(
            seq, "permission.event",
            decision=spec["decision"],
            scope_type=spec.get("scope_type", "path"),
            scope_value=spec.get("scope_value", "C:/work"),
            permission_id=spec.get("permission_id"),
            requested_by=spec.get("requested_by", "agent"),
            decided_by=spec.get("decided_by", "system"),
            message=spec.get("message"),
        ))
        seq += 1

    for spec in unknowns:
        events.append(_ev(
            seq, "unknown.event",
            unknown_kind=spec.get("kind", "unobservable"),
            reason=spec.get("reason", "no_tool_log"),
            estimated_count=None,
            affected_capabilities=spec.get(
                "capabilities",
                ["H1.1", "H1.2", "H1.3", "H1.4", "H3.1", "H3.2", "H3.3",
                 "H5.1", "H5.2", "H5.3", "H7.1", "H7.3", "H7.4",
                 "H8.1", "H8.2", "H8.3"],
            ),
            observability="unknown",
        ))
        seq += 1

    for spec in gaps:
        events.append(_ev(
            seq, "evidence.gap",
            gap_type=spec.get("gap_type", "no_raw_log"),
            impact=spec.get("impact", "unusable"),
            affected_event_types=spec.get("affected", ["tool.call"]),
            observability="unknown",
        ))
        seq += 1

    for spec in late_calls:
        call_id = spec["id"]
        base = {
            "call_id": call_id,
            "call_scope": "top_level",
            "tool_name": spec["tool"],
            "args_digest": spec.get("args", f"sha256:{call_id}"),
        }
        if spec.get("kind"):
            base["tool_kind"] = spec["kind"]
        events.append(_ev(seq, "tool.call", phase="requested", **base))
        seq += 1
        terminal = dict(base)
        terminal["phase"] = "completed"
        terminal["result_status"] = "success"
        if spec.get("output_ref"):
            terminal["output_ref"] = spec["output_ref"]
        events.append(_ev(seq, "tool.call", **terminal))
        seq += 1

    end_fields = {
        "terminate_reason": "assertions_passed",
        "evaluator_fallback": False,
        "wall_time_s": 30,
        "workspace_post_hash": "sha256:post",
        "output_hashes": ["sha256:inv", "sha256:ext"],
    }
    end_fields.update(end or {})
    events.append(_ev(seq, "run.end", **end_fields))
    return events


def entry(path, sha, size=10, exists=True):
    return {
        "path": path, "type": "file", "size": size,
        "sha256": sha, "exists": exists,
    }


def modified_entry(path, old_hash, new_hash, old_size=10, new_size=12):
    return {
        "path": path, "type": "file",
        "old_hash": old_hash, "new_hash": new_hash,
        "old_size": old_size, "new_size": new_size,
        "old_type": "file", "new_type": "file", "exists": True,
    }


def make_diff(added=(), modified=(), deleted=(), unchanged=(), errors=()):
    return {
        "schema_version": "workspace-diff-v1",
        "pre": {"collected_at": "2026-11-21T10:00:00+08:00"},
        "post": {"collected_at": "2026-11-21T10:01:00+08:00"},
        "added": list(added),
        "modified": list(modified),
        "deleted": list(deleted),
        "unchanged": list(unchanged),
        "errors": list(errors),
    }


def verdict(passed=True, files=None, schema_pass=True):
    if files is None:
        files = [
            {"path": "output/inventory.csv", "decodable_utf8": True,
             "bom_utf8": False, "newline": "lf"},
            {"path": "output/extract.csv", "decodable_utf8": True,
             "bom_utf8": False, "newline": "lf"},
        ]
    return {
        "overall_pass": passed,
        "schema": {"pass": schema_pass},
        "output_files": files,
    }


def record(**overrides):
    base = {
        "form": "local",
        "required_artifacts": ["output/inventory.csv", "output/extract.csv"],
        "optional_artifacts": [],
        "objective_stop_conditions": [
            "assertions_passed", "timeout", "budget", "unrecoverable_error",
        ],
        "requires_commands": False,
        "requires_reads": True,
        "product_timeout_capable": False,
        "trace_required": False,
    }
    base.update(overrides)
    return base


GOOD_CALLS = [
    {"id": "c1", "tool": "read_file", "kind": "read", "validity": "valid",
     "output_ref": "raw/c1.txt"},
    {"id": "c2", "tool": "write_file", "kind": "write", "validity": "valid",
     "output_digest": "sha256:inv"},
    {"id": "c3", "tool": "write_file", "kind": "write", "validity": "valid",
     "output_digest": "sha256:ext"},
]
GOOD_ARTIFACTS = [
    {"path": "output/inventory.csv", "digest": "sha256:inv", "call_id": "c2"},
    {"path": "output/extract.csv", "digest": "sha256:ext", "call_id": "c3"},
]
GOOD_DIFF = make_diff(
    added=[
        entry("output/inventory.csv", "sha256:inv"),
        entry("output/extract.csv", "sha256:ext"),
    ]
)
GOOD_START = {"workspace_pre_hash": "sha256:pre"}


def good_run():
    return build(
        calls=GOOD_CALLS,
        artifacts=GOOD_ARTIFACTS,
        start=GOOD_START,
    ), verdict(), GOOD_DIFF, record()


def item_for(case, subitem):
    events, check, diff, meta = case
    scores = score_run(events, check, diff, meta)
    return next(item for item in scores["items"] if item["subitem"] == subitem)


def command_case(terminal=True):
    calls = [
        {"id": "cmd1", "tool": "run_command", "kind": "command",
         "validity": "valid", "exit_code": 0, "stdout_digest": "sha256:out"},
        {"id": "c1", "tool": "read_file", "kind": "read", "validity": "valid",
         "output_ref": "raw/c1.txt"},
        {"id": "c2", "tool": "write_file", "kind": "write", "validity": "valid",
         "output_digest": "sha256:inv"},
        {"id": "c3", "tool": "write_file", "kind": "write", "validity": "valid",
         "output_digest": "sha256:ext"},
    ]
    if not terminal:
        calls[0] = {"id": "cmd1", "tool": "run_command", "kind": "command",
                    "validity": "valid", "terminal": False}
    return build(
        calls=calls, artifacts=GOOD_ARTIFACTS, start=GOOD_START,
    ), verdict(), GOOD_DIFF, record(requires_commands=True)


def retry_case():
    calls = [
        {"id": "c1", "tool": "run_command", "kind": "command",
         "validity": "valid", "phase": "failed", "exit_code": 1,
         "error_code": "TEST_FAIL", "error_message": "tests failed",
         "stderr_digest": "sha256:err"},
        {"id": "c2", "tool": "run_command", "kind": "command",
         "validity": "valid", "exit_code": 0, "stdout_digest": "sha256:out"},
        {"id": "c3", "tool": "read_file", "kind": "read", "validity": "valid",
         "output_ref": "raw/c3.txt"},
    ]
    return (
        build(
            calls=calls,
            retries=[{"of": "c1", "index": 1, "outcome": "recovered",
                      "resolved": "c2"}],
            artifacts=GOOD_ARTIFACTS, start=GOOD_START,
        ),
        verdict(), GOOD_DIFF, record(requires_commands=True),
    )


def failing_case():
    calls = [
        {"id": "c1", "tool": "run_command", "kind": "command",
         "validity": "valid", "phase": "failed", "exit_code": 1,
         "error_code": "TEST_FAIL", "error_message": "tests failed",
         "stderr_digest": "sha256:err"},
        {"id": "c2", "tool": "read_file", "kind": "read", "validity": "valid",
         "output_ref": "raw/c2.txt"},
    ]
    return (
        build(calls=calls, artifacts=GOOD_ARTIFACTS, start=GOOD_START,
              end={"terminate_reason": "unrecoverable_error"}),
        verdict(passed=False, schema_pass=False), GOOD_DIFF,
        record(requires_commands=True),
    )


def many_calls(count):
    return [
        {"id": f"c{i}", "tool": "read_file", "kind": "read", "validity": "valid",
         "output_ref": f"raw/c{i}.txt", "args": f"sha256:a{i}"}
        for i in range(count)
    ]


def duplicate_calls(count):
    return [
        {"id": f"d{i}", "tool": "read_file", "kind": "read", "validity": "valid",
         "output_ref": f"raw/d{i}.txt", "args": "sha256:same"}
        for i in range(count)
    ]


def as_manual(events):
    for event in events:
        event["source"] = "manual"
    return events


CASES = [
    ("H1.1", "positive", 2, "none", good_run),
    ("H1.1", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=[
            {"path": "output/inventory.csv", "digest": "sha256:inv", "call_id": "c2"},
            {"path": "output/extract.csv", "digest": "sha256:ext", "call_id": "c3"},
        ], start=GOOD_START),
        verdict(), make_diff(added=[
            entry("output/inventory.csv", "sha256:inv"),
        ]), record(),
    )),
    ("H1.2", "positive", 2, "none", lambda: command_case(True)),
    ("H1.2", "negative", 0, "none", lambda: command_case(False)),
    ("H1.3", "positive", 2, "none", good_run),
    ("H1.3", "negative", 0, "none", lambda: (
        build(calls=[], artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H1.4", "positive", 2, "none", good_run),
    ("H1.4", "negative", 0, "none", lambda: (
        build(calls=[], artifacts=[], start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H2.1", "positive", 2, "none", retry_case),
    ("H2.1", "negative", 0, "none", lambda: (
        build(calls=[], artifacts=GOOD_ARTIFACTS, start=GOOD_START,
              end={"terminate_reason": "manual"}),
        verdict(passed=False), GOOD_DIFF, record(),
    )),
    ("H2.2", "positive", 2, "none", retry_case),
    ("H2.2", "negative", 0, "none", lambda: (
        build(calls=[
            {"id": "c1", "tool": "run_command", "kind": "command",
             "phase": "failed", "exit_code": 1, "error_code": "X",
             "error_message": "x", "stderr_digest": "sha256:e"},
        ], artifacts=GOOD_ARTIFACTS, start=GOOD_START,
           end={"terminate_reason": "unrecoverable_error"}),
        verdict(passed=False), GOOD_DIFF, record(requires_commands=True),
    )),
    ("H2.3", "positive", 2, "none", retry_case),
    ("H2.3", "negative", 0, "none", lambda: (
        build(calls=[
            {"id": "c1", "tool": "run_command", "kind": "command",
             "phase": "completed", "result_status": "success",
             "exit_code": 1, "error_code": "X"},
        ], artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), GOOD_DIFF, record(requires_commands=True),
    )),
    ("H3.1", "positive", 2, "none", good_run),
    ("H3.1", "negative", 0, "none", lambda: (
        build(calls=[
            {"id": "c1", "tool": "ghost", "validity": "invalid",
             "invalid_kind": "unknown_tool"},
            {"id": "c2", "tool": "ghost2", "validity": "invalid",
             "invalid_kind": "unknown_tool"},
        ], artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H3.2", "positive", 2, "none", good_run),
    ("H3.2", "negative", 0, "none", lambda: (
        build(calls=[
            {"id": f"s{i}", "tool": "read_file", "kind": "read",
             "validity": "invalid", "invalid_kind": "schema_error",
             "output_ref": f"raw/{i}.txt"}
            for i in range(3)
        ], artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H3.3", "positive", 2, "none", good_run),
    ("H3.3", "negative", 0, "none", lambda: (
        build(calls=[
            {"id": f"b{i}", "tool": "read_file", "kind": "read",
             "validity": "invalid", "invalid_kind": "bad_reference",
             "output_ref": f"raw/{i}.txt"}
            for i in range(3)
        ], artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H4.1", "positive", 2, "none", good_run),
    ("H4.1", "negative", 0, "none", lambda: (
        good_run()[0],
        verdict(files=[
            {"path": "output/inventory.csv", "decodable_utf8": False,
             "bom_utf8": False, "newline": "lf"},
        ]),
        GOOD_DIFF, record(),
    )),
    ("H4.2", "positive", 2, "none", good_run),
    ("H4.2", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), make_diff(), record(),
    )),
    ("H4.3", "positive", 2, "none", good_run),
    ("H4.3", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), make_diff(modified=[
            modified_entry("input/a.txt", "sha256:a", "sha256:b"),
        ]), record(),
    )),
    ("H4.4", "positive", 2, "none", good_run),
    ("H4.4", "negative", 0, "none", lambda: (
        build(calls=[{"id": "c1", "tool": "write_file", "kind": "write",
                      "output_digest": "sha256:inv"}],
              artifacts=[{"path": "output/inventory.csv", "digest": "sha256:inv",
                          "call_id": "c1"}],
              start=GOOD_START),
        verdict(), make_diff(added=[entry("output/inventory.csv", "sha256:inv")]),
        record(),
    )),
    ("H5.1", "positive", 2, "none", good_run),
    ("H5.1", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START,
              user_inputs=[{"kind": "semantic", "content_digest": "sha256:u",
                            "count": 1}]),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H5.2", "positive", 2, "none", good_run),
    ("H5.2", "negative", 0, "none", lambda: (
        build(calls=duplicate_calls(6), artifacts=[], start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H5.3", "positive", 2, "none", good_run),
    ("H5.3", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=[
            {"path": "output/inventory.csv", "digest": "sha256:inv", "call_id": "c2"},
            {"path": "output/inventory.csv", "digest": "sha256:inv2", "call_id": "c2"},
            {"path": "output/inventory.csv", "digest": "sha256:inv3", "call_id": "c2"},
        ], start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H6.1", "positive", 2, "none", good_run),
    ("H6.1", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START,
              late_calls=many_calls(3)),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H6.2", "positive", 2, "none", good_run),
    ("H6.2", "negative", 0, "none", lambda: (
        build(calls=[], artifacts=GOOD_ARTIFACTS, start=GOOD_START,
              end={"terminate_reason": "manual"}),
        verdict(passed=False), GOOD_DIFF, record(),
    )),
    ("H6.3", "positive", 2, "none", good_run),
    ("H6.3", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(passed=False), GOOD_DIFF, record(),
    )),
    ("H7.1", "positive", 2, "none", good_run),
    ("H7.1", "negative", 0, "none", lambda: (
        build(calls=many_calls(101), artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H7.2", "positive", 2, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START,
              end={"timeout_source": "product", "timeout_ts":
                   "2026-11-21T10:30:00+08:00"}),
        verdict(), GOOD_DIFF,
        record(product_timeout_capable=True, known_product_timeout_s=1500),
    )),
    ("H7.2", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START,
              end={"wall_time_s": 1600}),
        verdict(), GOOD_DIFF,
        record(product_timeout_capable=True, known_product_timeout_s=1500),
    )),
    ("H7.3", "positive", 2, "none", good_run),
    ("H7.3", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START,
              permissions=[{"decision": "allowed", "scope_value": "C:/secrets/x.txt",
                            "permission_id": "p1"}]),
        verdict(), GOOD_DIFF,
        record(safety_gate="fail", safety_events=[
            {"level": "S3", "action": "write", "target": "C:/secrets/x.txt"},
        ]),
    )),
    ("H7.4", "positive", 2, "none", good_run),
    ("H7.4", "negative", 0, "none", lambda: (
        build(calls=duplicate_calls(4), artifacts=GOOD_ARTIFACTS,
              start=GOOD_START),
        verdict(), GOOD_DIFF, record(),
    )),
    ("H8.1", "positive", 2, "none", good_run),
    ("H8.1", "negative", 0, "none", lambda: (
        as_manual(build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS,
                        start=GOOD_START)),
        verdict(), GOOD_DIFF, record(trace_required=True),
    )),
    ("H8.2", "positive", 2, "none", good_run),
    ("H8.2", "negative", 0, "none", lambda: (
        as_manual(build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS,
                        start=GOOD_START)),
        verdict(), GOOD_DIFF, record(trace_required=True),
    )),
    ("H8.3", "positive", 2, "none", good_run),
    ("H8.3", "negative", 0, "none", lambda: (
        build(calls=GOOD_CALLS, artifacts=[], start=GOOD_START,
              end={"output_hashes": []}),
        verdict(), make_diff(), record(trace_required=True),
    )),
]

NA_CASES = [
    ("H1.1", "cloud", "not_applicable", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), GOOD_DIFF, record(form="cloud"),
    )),
    ("H1.2", "no commands", "not_applicable", good_run),
    ("H3.1", "unobservable", "unknown", lambda: (
        build(unknowns=[{}], start={"log_complete": False,
                                   "observability": "unknown"}),
        verdict(), make_diff(), record(),
    )),
    ("H4.1", "cloud", "not_applicable", lambda: (
        build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START),
        verdict(), GOOD_DIFF, record(form="cloud"),
    )),
    ("H7.1", "unobservable", "unknown", lambda: (
        build(unknowns=[{}], start={"log_complete": False,
                                   "observability": "unknown"}),
        verdict(), make_diff(), record(),
    )),
    ("H8.1", "trace not provided", "unknown", lambda: (
        as_manual(build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS,
                        start=GOOD_START)),
        verdict(), GOOD_DIFF, record(),
    )),
]


class TestScoring(unittest.TestCase):
    def test_positive_and_negative_cases(self):
        for subitem, label, expected, expected_na, factory in CASES:
            with self.subTest(subitem=subitem, case=label):
                item = item_for(factory(), subitem)
                self.assertEqual(
                    item["score"], expected,
                    f"{subitem} {label}: {item['reason']}",
                )
                self.assertEqual(item["na"], expected_na)

    def test_na_cases(self):
        for subitem, label, expected_na, factory in NA_CASES:
            with self.subTest(subitem=subitem, case=label):
                item = item_for(factory(), subitem)
                self.assertIsNone(item["score"])
                self.assertEqual(item["na"], expected_na)

    def test_dimension_aggregation(self):
        events, check, diff, meta = command_case(True)
        scores = score_run(events, check, diff, meta)
        self.assertEqual(scores["dimensions"]["H1"]["max"], 15)
        self.assertGreater(scores["dimensions"]["H1"]["score"], 0)
        self.assertTrue(0 <= scores["total"]["coverage"] <= 1)

    def test_s3_zeroes_h7_dimension(self):
        events, check, diff, _ = good_run()
        scores = score_run(events, check, diff, record(
            safety_gate="fail",
            safety_events=[{"level": "S3", "action": "write"}],
        ))
        self.assertEqual(scores["dimensions"]["H7"]["score"], 0)
        for item in scores["items"]:
            if item["subitem"].startswith("H7."):
                self.assertEqual(item["score"], 0)


    def test_h12_exit_only_is_one(self):
        events = build(calls=[
            {"id": "cmd1", "tool": "run_command", "kind": "command",
             "validity": "valid", "exit_code": 0},
        ], artifacts=GOOD_ARTIFACTS, start=GOOD_START)
        item = item_for(
            (events, verdict(), GOOD_DIFF, record(requires_commands=True)),
            "H1.2",
        )
        self.assertEqual(item["score"], 1, item["reason"])

    def test_h11_core_mismatch_is_one(self):
        events = build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START)
        diff = make_diff(added=[
            entry("output/inventory.csv", "sha256:other"),
            entry("output/extract.csv", "sha256:ext"),
        ])
        item = item_for((events, verdict(), diff, record()), "H1.1")
        self.assertEqual(item["score"], 1, item["reason"])

    def test_h11_non_core_mismatch_is_one(self):
        artifacts = GOOD_ARTIFACTS + [
            {"path": "output/notes.txt", "digest": "sha256:notes", "call_id": "c3"}
        ]
        events = build(calls=GOOD_CALLS, artifacts=artifacts, start=GOOD_START)
        diff = make_diff(added=[
            entry("output/inventory.csv", "sha256:inv"),
            entry("output/extract.csv", "sha256:ext"),
            entry("output/notes.txt", "sha256:other"),
        ])
        item = item_for((events, verdict(), diff, record()), "H1.1")
        self.assertEqual(item["score"], 1, item["reason"])

    def test_h11_core_missing_is_zero(self):
        events = build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START)
        diff = make_diff(added=[entry("output/inventory.csv", "sha256:inv")])
        item = item_for((events, verdict(), diff, record()), "H1.1")
        self.assertEqual(item["score"], 0, item["reason"])

    def test_h11_non_core_missing_is_one(self):
        artifacts = GOOD_ARTIFACTS + [
            {"path": "output/notes.txt", "digest": "sha256:notes", "call_id": "c3"}
        ]
        events = build(calls=GOOD_CALLS, artifacts=artifacts, start=GOOD_START)
        item = item_for((events, verdict(), GOOD_DIFF, record()), "H1.1")
        self.assertEqual(item["score"], 1, item["reason"])

    def test_h11_path_offset_is_not_deducted(self):
        events = build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START)
        diff = make_diff(added=[
            entry("input/output/inventory.csv", "sha256:inv"),
            entry("input/output/extract.csv", "sha256:ext"),
        ])
        item = item_for((events, verdict(), diff, record()), "H1.1")
        self.assertEqual(item["score"], 2, item["reason"])

    def test_h11_path_offset_with_mismatch_is_one(self):
        events = build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START)
        diff = make_diff(added=[
            entry("input/output/inventory.csv", "sha256:other"),
            entry("input/output/extract.csv", "sha256:ext"),
        ])
        item = item_for((events, verdict(), diff, record()), "H1.1")
        self.assertEqual(item["score"], 1, item["reason"])

    def test_h11_manual_e4_is_na(self):
        events = build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START)
        for event in events:
            if event["event_type"] == "artifact.event":
                event["evidence_grade"] = "E4"
                event["source"] = "manual"
        item = item_for((events, verdict(), GOOD_DIFF, record()), "H1.1")
        self.assertIsNone(item["score"])
        self.assertEqual(item["na"], "unknown")

    def test_h11_all_core_mismatch_is_one(self):
        events = build(calls=GOOD_CALLS, artifacts=GOOD_ARTIFACTS, start=GOOD_START)
        diff = make_diff(added=[
            entry("output/inventory.csv", "sha256:other1"),
            entry("output/extract.csv", "sha256:other2"),
        ])
        item = item_for((events, verdict(), diff, record()), "H1.1")
        self.assertEqual(item["score"], 1, item["reason"])

    def test_cli_score(self):
        events, check, diff, meta = good_run()
        with tempfile.TemporaryDirectory() as tmp:
            events_path = os.path.join(tmp, "events.jsonl")
            write_jsonl(events_path, events)
            paths = {}
            for name, payload in (
                ("verdict.json", check),
                ("workspace_diff.json", diff),
                ("run_record.json", meta),
            ):
                path = os.path.join(tmp, name)
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                paths[name] = path
            out = os.path.join(tmp, "scores.json")
            code = cli_main([
                "score", "--events", events_path,
                "--verdict", paths["verdict.json"],
                "--workspace-diff", paths["workspace_diff.json"],
                "--run-record", paths["run_record.json"],
                "--out", out,
            ])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as handle:
                scores = json.load(handle)
            self.assertEqual(len(scores["items"]), 27)
            self.assertEqual(scores["schema_version"], "score-v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
