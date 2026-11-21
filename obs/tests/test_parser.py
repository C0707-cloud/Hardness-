"""Synthetic-log tests for obs-v1 parser. No real product data."""

import unittest

from obs.parser import (
    ParseError,
    parse_lines,
    summarize,
    validate_event,
    validate_events,
)

SCENARIOS = [
    {
        "name": "1 成功工具调用",
        "lines": """
2026-11-21T10:00:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; capabilities={}; workspace_pre_hash=sha256:pre
2026-11-21T10:00:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:aaa; args_summary={"path":"input/报表.txt"}; result_status=pending
2026-11-21T10:00:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:aaa; result_status=success; exit_code=0; output_digest=sha256:bbb
2026-11-21T10:00:03+08:00 | artifact.event | op=create; path=output/extract.csv; content_digest=sha256:ccc; size=128; encoding=utf-8; newline=LF; bom=false; call_id=c1
2026-11-21T10:00:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4; workspace_post_hash=sha256:post; output_hashes=["sha256:out1"]
""".strip().splitlines(),
        "manifest": {"log_complete": True},
        "expect": {
            "tool_calls_visible": 1,
            "retry_internal": 0,
            "tool_failures": 0,
            "tool_calls_duplicate": 0,
            "permission_blocked": 0,
            "unknown_calls": 0,
            "user_inputs": 0,
            "terminate_reason": "assertions_passed",
            "calls_after_end": 0,
            "workspace_hash_complete": True,
            "artifact_count": 1,
            "artifacts_without_call_id": [],
            "tool_calls_visibility": "complete",
            "tool_calls_incomplete": 0,
            "tool_calls_unknown": 0,
        },
    },
    {
        "name": "2 非零返回码",
        "lines": """
2026-11-21T10:01:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true
2026-11-21T10:01:01+08:00 | tool.call | phase=requested; call_id=c2; call_scope=top_level; tool_kind=command; tool_name=run_command; args_digest=sha256:d2
2026-11-21T10:01:02+08:00 | tool.call | phase=completed; call_id=c2; call_scope=top_level; tool_kind=command; tool_name=run_command; args_digest=sha256:d2; result_status=error; exit_code=1; error_code=EXIT_1; stdout_digest=sha256:o2; stderr_digest=sha256:e2
2026-11-21T10:01:03+08:00 | run.end | terminate_reason=unrecoverable_error; evaluator_fallback=false; wall_time_s=3
""".strip().splitlines(),
        "manifest": {"log_complete": True},
        "expect": {
            "tool_calls_visible": 1,
            "tool_failures": 1,
            "terminate_reason": "unrecoverable_error",
            "command_calls": 1,
            "command_calls_missing_exit_code": 0,
            "command_calls_missing_output": 0,
        },
    },
    {
        "name": "3 工具失败后重试",
        "lines": """
2026-11-21T10:02:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true
2026-11-21T10:02:01+08:00 | tool.call | phase=requested; call_id=c3; call_scope=top_level; tool_name=read_file; args_digest=sha256:f3
2026-11-21T10:02:02+08:00 | tool.call | phase=failed; call_id=c3; call_scope=top_level; tool_name=read_file; args_digest=sha256:f3; result_status=error; error_code=DECODE_ERROR
2026-11-21T10:02:03+08:00 | tool.retry | retry_of_call_id=c3; retry_scope=internal; retry_index=1; retry_reason=decode_error; result_status=success
2026-11-21T10:02:04+08:00 | tool.call | phase=requested; call_id=c4; call_scope=top_level; tool_name=read_file; args_digest=sha256:f4
2026-11-21T10:02:05+08:00 | tool.call | phase=completed; call_id=c4; call_scope=top_level; tool_name=read_file; args_digest=sha256:f4; result_status=success; exit_code=0
2026-11-21T10:02:06+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=6
""".strip().splitlines(),
        "manifest": {"log_complete": True},
        "expect": {
            "tool_calls_visible": 2,
            "retry_internal": 1,
            "retry_top_level": 0,
            "tool_failures": 1,
        },
    },
    {
        "name": "4 重复调用",
        "lines": """
2026-11-21T10:03:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true
2026-11-21T10:03:01+08:00 | tool.call | phase=requested; call_id=c5; call_scope=top_level; tool_name=read_file; args_digest=sha256:same
2026-11-21T10:03:02+08:00 | tool.call | phase=completed; call_id=c5; call_scope=top_level; tool_name=read_file; args_digest=sha256:same; result_status=success
2026-11-21T10:03:03+08:00 | tool.call | phase=requested; call_id=c6; call_scope=top_level; tool_name=read_file; args_digest=sha256:same
2026-11-21T10:03:04+08:00 | tool.call | phase=completed; call_id=c6; call_scope=top_level; tool_name=read_file; args_digest=sha256:same; result_status=success
2026-11-21T10:03:05+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=5
""".strip().splitlines(),
        "manifest": {"log_complete": True},
        "expect": {
            "tool_calls_visible": 2,
            "tool_calls_duplicate": 1,
            "duplicate_call_ids": ["c6"],
        },
    },
    {
        "name": "5 禁止路径访问尝试",
        "lines": """
2026-11-21T10:04:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true
2026-11-21T10:04:01+08:00 | tool.call | phase=requested; call_id=c7; call_scope=top_level; tool_name=read_file; args_digest=sha256:secret
2026-11-21T10:04:02+08:00 | tool.call | phase=rejected; call_id=c7; call_scope=top_level; tool_name=read_file; args_digest=sha256:secret; result_status=denied; reject_reason=permission_denied
2026-11-21T10:04:03+08:00 | permission.event | decision=blocked; scope_type=path; scope_value=C:/eval/secrets/; requested_by=agent; decided_by=product; message=outside workspace
2026-11-21T10:04:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4
""".strip().splitlines(),
        "manifest": {"log_complete": True},
        "expect": {
            "tool_calls_visible": 1,
            "permission_blocked": 1,
            "rejected_calls": 1,
        },
    },
    {
        "name": "6 工具不可见，只能记 UNKNOWN",
        "lines": """
2026-11-21T10:05:00+08:00 | run.start | product=元宝; task_id=T1; log_complete=false; capabilities={"H7.2":"not_applicable"}
2026-11-21T10:05:01+08:00 | unknown.event | unknown_kind=unknown; reason=no_tool_log; affected_capability=H7.1; estimated_count=null
2026-11-21T10:05:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2
""".strip().splitlines(),
        "manifest": {"log_complete": False},
        "expect": {
            "tool_calls_visible": 0,
            "unknown_events": 1,
            "unknown_calls": None,
            "na_unknown": ["H7.1"],
            "na_not_applicable": ["H7.2"],
            "tool_calls_visibility": "unknown",
            "tool_failures": None,
            "permission_blocked": None,
        },
    },
    {
        "name": "7 用户澄清，不应算工具调用",
        "lines": """
2026-11-21T10:06:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true
2026-11-21T10:06:01+08:00 | user.input | kind=semantic; content_digest=sha256:u1; count=1; turn_index=1
2026-11-21T10:06:02+08:00 | tool.call | phase=requested; call_id=c8; call_scope=top_level; tool_name=read_file; args_digest=sha256:g7
2026-11-21T10:06:03+08:00 | tool.call | phase=completed; call_id=c8; call_scope=top_level; tool_name=read_file; args_digest=sha256:g7; result_status=success
2026-11-21T10:06:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4
""".strip().splitlines(),
        "manifest": {"log_complete": True},
        "expect": {
            "tool_calls_visible": 1,
            "user_inputs": 1,
        },
    },
    {
        "name": "8 超时或停止事件",
        "lines": """
2026-11-21T10:07:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true
2026-11-21T10:07:01+08:00 | tool.call | phase=requested; call_id=c9; call_scope=top_level; tool_name=read_file; args_digest=sha256:h8
2026-11-21T10:07:02+08:00 | tool.call | phase=completed; call_id=c9; call_scope=top_level; tool_name=read_file; args_digest=sha256:h8; result_status=success
2026-11-21T10:07:03+08:00 | run.end | terminate_reason=timeout; evaluator_fallback=true; wall_time_s=1500; timeout_source=evaluator; timeout_ts=2026-11-21T10:07:03+08:00
2026-11-21T10:07:04+08:00 | tool.call | phase=requested; call_id=c10; call_scope=top_level; tool_name=read_file; args_digest=sha256:h9
""".strip().splitlines(),
        "manifest": {"log_complete": True},
        "expect": {
            "tool_calls_visible": 2,
            "terminate_reason": "timeout",
            "timeout_hit": True,
            "timeout_source": "evaluator",
            "product_timeout_hit": False,
            "evaluator_timeout_hit": True,
            "tool_calls_after_timeout": 1,
            "evaluator_fallback": True,
            "calls_after_end": 1,
            "tool_calls_visibility": "partial",
            "tool_calls_incomplete": 1,
            "tool_calls_unknown": None,
        },
    },
]


class TestParserSynthetic(unittest.TestCase):
    def test_all_scenarios(self):
        for scenario in SCENARIOS:
            with self.subTest(name=scenario["name"]):
                events = parse_lines(scenario["lines"], run_id="synthetic")
                summary = summarize(events, scenario["manifest"])
                for key, expected in scenario["expect"].items():
                    self.assertEqual(
                        summary[key],
                        expected,
                        f"{scenario['name']} / {key}",
                    )

    def test_common_envelope(self):
        events = parse_lines(SCENARIOS[0]["lines"], run_id="r1")
        for event in events:
            for field in (
                "schema_version", "event_id", "run_id", "session_id",
                "seq", "ts", "event_type", "source", "observability",
                "actor", "evidence_grade",
            ):
                self.assertIn(field, event)

    def test_unknown_estimated_count_is_null(self):
        events = parse_lines(SCENARIOS[5]["lines"], run_id="r1")
        unknown = next(e for e in events if e["event_type"] == "unknown.event")
        self.assertIsNone(unknown["estimated_count"])

    def test_subcall_not_counted_as_top_level(self):
        lines = [
            "2026-11-21T10:08:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:08:01+08:00 | tool.call | phase=requested; call_id=s1; call_scope=subcall; tool_name=read_file; args_digest=sha256:s1",
            "2026-11-21T10:08:02+08:00 | tool.call | phase=completed; call_id=s1; call_scope=subcall; tool_name=read_file; args_digest=sha256:s1; result_status=success",
            "2026-11-21T10:08:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_visible"], 0)
        self.assertEqual(summary["tool_calls_subcall"], 1)

    def test_top_level_retry(self):
        lines = [
            "2026-11-21T10:09:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:09:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:09:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:09:03+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=top_level; retry_index=1; retry_reason=manual_retry; result_status=success",
            "2026-11-21T10:09:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["retry_top_level"], 1)
        self.assertEqual(summary["retry_internal"], 0)

    def test_evidence_gap(self):
        lines = [
            '2026-11-21T10:10:00+08:00 | evidence.gap | gap_type=missing_args; affected_event_types=["tool.call"]; impact=partial',
        ]
        summary = summarize(parse_lines(lines, strict=False))
        self.assertEqual(summary["evidence_gaps"], ["missing_args"])

    def test_parse_rejects_missing_phase(self):
        with self.assertRaises(ParseError):
            parse_lines(["2026-11-21T10:11:00+08:00 | tool.call | call_id=bad"])

    def test_parse_rejects_unknown_event_type(self):
        with self.assertRaises(ParseError):
            parse_lines(["2026-11-21T10:11:00+08:00 | foo.bar | x=1"])

    def test_validate_event_reports_missing_call_id(self):
        event = {
            "schema_version": "obs-v1",
            "event_id": "EV-r-000001",
            "run_id": "r",
            "session_id": "s",
            "seq": 1,
            "ts": "2026-11-21T10:00:00+08:00",
            "event_type": "tool.call",
            "source": "manual",
            "observability": "visible",
            "actor": "agent",
            "evidence_grade": "E3",
            "phase": "requested",
            "call_scope": "top_level",
            "tool_name": "read_file",
            "args_digest": "sha256:x",
        }
        errors = validate_event(event)
        self.assertTrue(any("call_id" in error for error in errors))

    def test_validate_event_reports_bad_enum(self):
        event = {
            "schema_version": "obs-v1",
            "event_id": "EV-r-000001",
            "run_id": "r",
            "session_id": "s",
            "seq": 1,
            "ts": "2026-11-21T10:00:00+08:00",
            "event_type": "run.end",
            "source": "bad_source",
            "observability": "visible",
            "actor": "evaluator",
            "evidence_grade": "E3",
            "terminate_reason": "assertions_passed",
            "evaluator_fallback": False,
            "wall_time_s": 1,
        }
        errors = validate_event(event)
        self.assertTrue(any("bad source" in error for error in errors))

    def test_validate_events_reports_duplicate_event_id(self):
        def run_start(seq, event_id):
            return {
                "schema_version": "obs-v1",
                "event_id": event_id,
                "run_id": "r",
                "session_id": "s",
                "seq": seq,
                "ts": "2026-11-21T10:00:00+08:00",
                "event_type": "run.start",
                "source": "manual",
                "observability": "visible",
                "actor": "evaluator",
                "evidence_grade": "E3",
                "product": "Codex",
                "task_id": "T1",
                "log_complete": True,
                "capabilities": {},
            }

        report = validate_events([run_start(1, "same"), run_start(2, "same")])
        self.assertTrue(any("duplicate event_id" in error for error in report["errors"]))

    def test_validate_events_reports_non_monotonic_seq(self):
        def run_start(seq, event_id):
            return {
                "schema_version": "obs-v1",
                "event_id": event_id,
                "run_id": "r",
                "session_id": "s",
                "seq": seq,
                "ts": "2026-11-21T10:00:00+08:00",
                "event_type": "run.start",
                "source": "manual",
                "observability": "visible",
                "actor": "evaluator",
                "evidence_grade": "E3",
                "product": "Codex",
                "task_id": "T1",
                "log_complete": True,
                "capabilities": {},
            }

        report = validate_events([run_start(2, "a"), run_start(1, "b")])
        self.assertTrue(any("seq must be strictly increasing" in error for error in report["errors"]))

    def test_invalid_call_distinct_from_execution_failure(self):
        lines = [
            "2026-11-21T10:12:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:12:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; validity=invalid; invalid_kind=type_error",
            "2026-11-21T10:12:02+08:00 | tool.call | phase=rejected; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=denied; reject_reason=schema_error",
            "2026-11-21T10:12:03+08:00 | tool.call | phase=requested; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:b; validity=valid",
            "2026-11-21T10:12:04+08:00 | tool.call | phase=failed; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:b; result_status=error; error_code=EXIT_1",
            "2026-11-21T10:12:05+08:00 | run.end | terminate_reason=unrecoverable_error; evaluator_fallback=false; wall_time_s=5",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_invalid"], 1)
        self.assertEqual(summary["tool_failures"], 1)
        self.assertEqual(summary["tool_calls_invalid_unknown"], 0)

    def test_unnecessary_counts(self):
        lines = [
            "2026-11-21T10:13:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:13:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; unnecessary=true; unnecessary_kind=irrelevant",
            "2026-11-21T10:13:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:13:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_unnecessary"], 1)
        self.assertEqual(summary["tool_calls_unnecessary_unknown"], 0)

    def test_artifact_orphan(self):
        lines = [
            "2026-11-21T10:14:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:14:01+08:00 | artifact.event | op=create; path=output/orphan.csv; content_digest=sha256:x",
            "2026-11-21T10:14:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["artifact_count"], 1)
        self.assertEqual(summary["artifacts_without_call_id"], ["output/orphan.csv"])

    def test_evidence_gap_details(self):
        lines = [
            '2026-11-21T10:15:00+08:00 | evidence.gap | gap_type=missing_args; affected_event_types=["tool.call"]; affected_event_ids=["EV-r-000003"]; affected_seqs=[3]; impact=partial',
        ]
        summary = summarize(parse_lines(lines, strict=False))
        detail = summary["evidence_gap_details"][0]
        self.assertEqual(detail["affected_event_ids"], ["EV-r-000003"])
        self.assertEqual(detail["affected_seqs"], [3])

    def test_command_missing_metrics(self):
        lines = [
            "2026-11-21T10:16:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:16:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_kind=command; tool_name=run_command; args_digest=sha256:a",
            "2026-11-21T10:16:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_kind=command; tool_name=run_command; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:16:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["command_calls"], 1)
        self.assertEqual(summary["command_calls_missing_exit_code"], 1)
        self.assertEqual(summary["command_calls_missing_output"], 1)

    def test_product_timeout_source(self):
        lines = [
            "2026-11-21T10:17:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:17:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:17:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:17:03+08:00 | run.end | terminate_reason=timeout; evaluator_fallback=false; wall_time_s=1500; timeout_source=product; timeout_ts=2026-11-21T10:17:03+08:00",
        ]
        summary = summarize(parse_lines(lines))
        self.assertTrue(summary["product_timeout_hit"])
        self.assertFalse(summary["evaluator_timeout_hit"])

    def test_tool_calls_total_and_visible(self):
        summary = summarize(parse_lines(SCENARIOS[2]["lines"]))
        self.assertEqual(summary["tool_calls_total"], 2)
        self.assertEqual(summary["tool_calls_visible"], 2)

    def test_terminal_only_logs_count_calls(self):
        lines = [
            "2026-11-21T10:18:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:18:01+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:18:02+08:00 | tool.call | phase=completed; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:b; result_status=success",
            "2026-11-21T10:18:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_total"], 2)
        self.assertEqual(summary["tool_calls_visible"], 2)
        self.assertEqual(summary["tool_calls_missing_request"], 2)
        self.assertEqual(summary["tool_calls_incomplete"], 2)
        self.assertEqual(summary["tool_calls_visibility"], "partial")
        self.assertIsNone(summary["tool_calls_unknown"])

    def test_unknown_visibility_sets_derived_counts_null(self):
        lines = [
            "2026-11-21T10:19:00+08:00 | run.start | product=元宝; task_id=T1; log_complete=false",
            "2026-11-21T10:19:01+08:00 | unknown.event | unknown_kind=unknown; reason=no_tool_log; affected_capability=H7.1; estimated_count=null",
            "2026-11-21T10:19:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_visibility"], "unknown")
        self.assertIsNone(summary["tool_failures"])
        self.assertIsNone(summary["permission_blocked"])
        self.assertIsNone(summary["rejected_calls"])
        self.assertIsNone(summary["tool_calls_duplicate"])
        self.assertIsNone(summary["tool_calls_invalid"])
        self.assertIsNone(summary["tool_calls_unnecessary"])

    def test_requested_without_terminal_is_incomplete(self):
        lines = [
            "2026-11-21T10:20:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:20:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:20:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_missing_terminal"], 1)
        self.assertEqual(summary["tool_calls_incomplete"], 1)
        self.assertEqual(summary["tool_calls_visibility"], "partial")
        self.assertIsNone(summary["tool_calls_unknown"])
        self.assertFalse(summary["derived_counts_complete"])

    def test_partial_visibility_with_minor_unknown(self):
        lines = [
            "2026-11-21T10:21:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:21:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:21:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:21:03+08:00 | unknown.event | unknown_kind=unobservable; reason=internal_only; affected_capability=H7.4; estimated_count=null",
            "2026-11-21T10:21:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_visible"], 1)
        self.assertEqual(summary["unknown_events"], 1)
        self.assertIsNone(summary["tool_calls_unknown"])
        self.assertEqual(summary["tool_calls_visibility"], "partial")
        self.assertTrue(summary["tool_calls_partial"])

    def test_user_input_breakdown_and_count(self):
        lines = [
            "2026-11-21T10:22:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:22:01+08:00 | user.input | kind=semantic; count=1; content_digest=sha256:u1",
            "2026-11-21T10:22:02+08:00 | user.input | kind=mechanical; count=3; content_digest=sha256:u2",
            "2026-11-21T10:22:03+08:00 | user.input | kind=control; count=1; content_digest=sha256:u3",
            "2026-11-21T10:22:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["semantic_inputs"], 1)
        self.assertEqual(summary["mechanical_continues"], 3)
        self.assertEqual(summary["control_actions"], 1)
        self.assertEqual(summary["user_inputs"], 5)
        self.assertEqual(summary["user_input_events"], 3)

    def test_user_input_count_five(self):
        lines = [
            "2026-11-21T10:23:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:23:01+08:00 | user.input | kind=mechanical; count=5; content_digest=sha256:u1",
            "2026-11-21T10:23:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["mechanical_continues"], 5)
        self.assertEqual(summary["user_inputs"], 5)
        self.assertEqual(summary["user_input_events"], 1)

    def test_permission_pending_and_response(self):
        lines = [
            "2026-11-21T10:24:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:24:01+08:00 | permission.event | decision=requested; permission_id=p1; scope_type=path; scope_value=C:/x; requested_by=agent; decided_by=product",
            "2026-11-21T10:24:02+08:00 | user.input | kind=control; permission_ref=p1; count=1; content_digest=sha256:u1",
            "2026-11-21T10:24:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["permission_requested"], 1)
        self.assertEqual(summary["permission_pending"], 0)
        self.assertEqual(summary["permission_responses"], 1)
        self.assertEqual(summary["control_actions"], 1)
        self.assertEqual(summary["semantic_inputs"], 0)

    def test_permission_pending_unresolved(self):
        lines = [
            "2026-11-21T10:25:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:25:01+08:00 | permission.event | decision=requested; permission_id=p1; scope_type=path; scope_value=C:/x; requested_by=agent; decided_by=product",
            "2026-11-21T10:25:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["permission_pending"], 1)
        self.assertEqual(summary["permission_responses"], 0)

    def test_permission_resolved_by_decision(self):
        lines = [
            "2026-11-21T10:26:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:26:01+08:00 | permission.event | decision=requested; permission_id=p1; scope_type=path; scope_value=C:/x; requested_by=agent; decided_by=product",
            "2026-11-21T10:26:02+08:00 | permission.event | decision=granted; permission_id=p1; scope_type=path; scope_value=C:/x; requested_by=agent; decided_by=user",
            "2026-11-21T10:26:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["permission_requested"], 1)
        self.assertEqual(summary["permission_granted"], 1)
        self.assertEqual(summary["permission_pending"], 0)

    def test_permission_without_id_is_pending(self):
        lines = [
            "2026-11-21T10:27:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:27:01+08:00 | permission.event | decision=requested; scope_type=path; scope_value=C:/x; requested_by=agent; decided_by=product",
            "2026-11-21T10:27:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["permission_pending"], 1)
        self.assertTrue(any("permission_id" in warning for warning in summary["warnings"]))

    def test_retry_outcomes(self):
        lines = [
            "2026-11-21T10:28:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:28:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:28:02+08:00 | tool.call | phase=failed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=error; error_code=E1",
            "2026-11-21T10:28:03+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=internal; retry_index=1; retry_reason=r; result_status=success; retry_outcome=recovered; resolved_call_id=c1; recovered=true",
            "2026-11-21T10:28:04+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=internal; retry_index=2; retry_reason=r; result_status=error; retry_outcome=failed; recovered=false",
            "2026-11-21T10:28:05+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=internal; retry_index=3; retry_reason=r; result_status=success",
            "2026-11-21T10:28:06+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=6",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["retries_recovered"], 1)
        self.assertEqual(summary["retries_failed"], 1)
        self.assertEqual(summary["retries_unknown"], 1)
        self.assertEqual(summary["retry_chains"], 1)
        self.assertEqual(summary["retry_chain_unknown"], 1)

    def test_top_level_retry_counts_in_budget(self):
        lines = [
            "2026-11-21T10:29:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:29:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:29:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:29:03+08:00 | tool.call | phase=requested; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:b",
            "2026-11-21T10:29:04+08:00 | tool.call | phase=completed; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:b; result_status=success",
            "2026-11-21T10:29:05+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=top_level; retry_index=1; retry_reason=r; result_status=success",
            "2026-11-21T10:29:06+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=top_level; retry_index=2; retry_reason=r; result_status=success; resolved_call_id=c2; retry_outcome=recovered",
            "2026-11-21T10:29:07+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=7",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_total"], 3)
        self.assertEqual(summary["tool_calls_visible"], 3)
        self.assertEqual(summary["retry_top_level"], 2)
        self.assertEqual(summary["tool_calls_from_top_level_retry"], 1)

    def test_failure_signals(self):
        lines = [
            "2026-11-21T10:30:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:30:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_kind=command; tool_name=run_command; args_digest=sha256:a",
            "2026-11-21T10:30:02+08:00 | tool.call | phase=failed; call_id=c1; call_scope=top_level; tool_kind=command; tool_name=run_command; args_digest=sha256:a; result_status=error; exit_code=1; error_code=E1",
            "2026-11-21T10:30:03+08:00 | run.end | terminate_reason=unrecoverable_error; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_failures"], 1)
        self.assertEqual(summary["tool_failure_signals"], 3)
        self.assertEqual(summary["failure_signal_counts"]["phase_failed"], 1)
        self.assertEqual(summary["failure_signal_counts"]["result_status_error"], 1)
        self.assertEqual(summary["failure_signal_counts"]["exit_code_nonzero"], 1)

    def test_failure_deduped_by_call_id(self):
        lines = [
            "2026-11-21T10:31:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:31:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:31:02+08:00 | tool.call | phase=failed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=error; error_code=E1",
            "2026-11-21T10:31:03+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=error; exit_code=1",
            "2026-11-21T10:31:04+08:00 | run.end | terminate_reason=unrecoverable_error; evaluator_fallback=false; wall_time_s=4",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_failures"], 1)

    def test_retry_chain_integrity(self):
        lines = [
            "2026-11-21T10:32:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:32:01+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=internal; retry_index=1; retry_reason=r; result_status=error; retry_outcome=failed",
            "2026-11-21T10:32:02+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=internal; retry_index=2; retry_reason=r; result_status=success; retry_outcome=recovered",
            "2026-11-21T10:32:03+08:00 | tool.retry | retry_of_call_id=c2; retry_scope=internal; retry_index=1; retry_reason=r; result_status=error",
            "2026-11-21T10:32:04+08:00 | tool.retry | retry_of_call_id=c2; retry_scope=internal; retry_index=3; retry_reason=r; result_status=error",
            "2026-11-21T10:32:05+08:00 | tool.retry | retry_of_call_id=c3; retry_scope=internal; retry_index=1; retry_reason=r; result_status=error",
            "2026-11-21T10:32:06+08:00 | tool.retry | retry_of_call_id=c3; retry_scope=internal; retry_index=1; retry_reason=r; result_status=error",
            "2026-11-21T10:32:07+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=7",
        ]
        events = parse_lines(lines, strict=False)
        summary = summarize(events)
        self.assertEqual(summary["retry_chains"], 3)
        self.assertEqual(summary["retry_chain_incomplete"], 2)
        self.assertEqual(summary["retry_chain_recovered"], 1)
        self.assertEqual(summary["retry_chain_unknown"], 2)
        report = validate_events(events)
        self.assertTrue(any("retry_index gap for c2" in warning for warning in report["warnings"]))
        self.assertTrue(any("duplicate retry_index for c3" in warning for warning in report["warnings"]))

    def test_dual_source_same_call_id_is_deduplicated(self):
        lines = [
            "2026-11-21T10:33:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; authoritative_source=product_log",
            "2026-11-21T10:33:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=terminal_capture",
            "2026-11-21T10:33:02+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=product_log",
            "2026-11-21T10:33:03+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; source=product_log",
            "2026-11-21T10:33:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_total"], 1)
        self.assertEqual(summary["tool_calls_visible"], 1)
        self.assertEqual(summary["tool_calls_duplicate"], 0)
        self.assertEqual(summary["call_id_collisions"], 1)
        self.assertEqual(summary["authoritative_source"], "product_log")

    def test_missing_args_key_skips_duplicate_check(self):
        lines = [
            "2026-11-21T10:34:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:34:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file",
            "2026-11-21T10:34:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; result_status=success",
            "2026-11-21T10:34:03+08:00 | tool.call | phase=requested; call_id=c2; call_scope=top_level; tool_name=read_file",
            "2026-11-21T10:34:04+08:00 | tool.call | phase=completed; call_id=c2; call_scope=top_level; tool_name=read_file; result_status=success",
            "2026-11-21T10:34:05+08:00 | tool.call | phase=requested; call_id=c3; call_scope=top_level; tool_name=read_file",
            "2026-11-21T10:34:06+08:00 | tool.call | phase=completed; call_id=c3; call_scope=top_level; tool_name=read_file; result_status=success",
            "2026-11-21T10:34:07+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=7",
        ]
        summary = summarize(parse_lines(lines, strict=False))
        self.assertEqual(summary["tool_calls_duplicate"], 0)
        self.assertEqual(summary["duplicate_check_skipped"], 3)
        self.assertFalse(summary["duplicate_check_complete"])

    def test_args_summary_fallback_for_duplicates(self):
        lines = [
            "2026-11-21T10:35:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:35:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_summary={\"path\":\"a\"}",
            "2026-11-21T10:35:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_summary={\"path\":\"a\"}; result_status=success",
            "2026-11-21T10:35:03+08:00 | tool.call | phase=requested; call_id=c2; call_scope=top_level; tool_name=read_file; args_summary={\"path\":\"a\"}",
            "2026-11-21T10:35:04+08:00 | tool.call | phase=completed; call_id=c2; call_scope=top_level; tool_name=read_file; args_summary={\"path\":\"a\"}; result_status=success",
            "2026-11-21T10:35:05+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=5",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_duplicate"], 1)
        self.assertEqual(summary["duplicate_check_skipped"], 0)

    def test_retry_with_same_args_not_duplicate(self):
        lines = [
            "2026-11-21T10:36:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:36:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:same",
            "2026-11-21T10:36:02+08:00 | tool.call | phase=failed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:same; result_status=error; error_code=E1",
            "2026-11-21T10:36:03+08:00 | tool.call | phase=requested; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:same",
            "2026-11-21T10:36:04+08:00 | tool.call | phase=completed; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:same; result_status=success",
            "2026-11-21T10:36:05+08:00 | tool.retry | retry_of_call_id=c1; retry_scope=top_level; retry_index=1; retry_reason=r; result_status=success; resolved_call_id=c2; retry_outcome=recovered",
            "2026-11-21T10:36:06+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=6",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_duplicate"], 0)
        self.assertEqual(summary["duplicate_retry_excluded"], 2)

    def test_multiple_runs_are_rejected(self):
        lines = [
            "2026-11-21T10:37:00+08:00 | run.start | run_id=runA; session_id=sA; product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:37:01+08:00 | run.start | run_id=runB; session_id=sB; product=Codex; task_id=T1; log_complete=true",
        ]
        events = parse_lines(lines, strict=False)
        report = validate_events(events)
        self.assertTrue(any("multiple run_id" in error for error in report["errors"]))
        self.assertTrue(any("multiple session_id" in error for error in report["errors"]))
        with self.assertRaises(ParseError):
            summarize(events)

    def test_duplicate_seq_is_rejected(self):
        def run_start(seq, event_id):
            return {
                "schema_version": "obs-v1",
                "event_id": event_id,
                "run_id": "r",
                "session_id": "s",
                "seq": seq,
                "ts": "2026-11-21T10:00:00+08:00",
                "event_type": "run.start",
                "source": "manual",
                "observability": "visible",
                "actor": "evaluator",
                "evidence_grade": "E3",
                "product": "Codex",
                "task_id": "T1",
                "log_complete": True,
                "capabilities": {},
            }

        report = validate_events([run_start(1, "a"), run_start(1, "b")])
        self.assertTrue(any("seq must be strictly increasing" in error for error in report["errors"]))

    def test_unknown_observability_blocks_complete(self):
        lines = [
            "2026-11-21T10:40:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:40:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; observability=unknown",
            "2026-11-21T10:40:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; observability=unknown",
            "2026-11-21T10:40:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_total"], 1)
        self.assertEqual(summary["tool_calls_visible"], 0)
        self.assertEqual(summary["tool_calls_visibility"], "unknown")
        self.assertIsNone(summary["tool_calls_unknown"])
        self.assertFalse(summary["derived_counts_complete"])

    def test_reused_call_id_is_split(self):
        lines = [
            "2026-11-21T10:41:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:41:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:41:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:41:03+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=write_file; args_digest=sha256:b",
            "2026-11-21T10:41:04+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=write_file; args_digest=sha256:b; result_status=success",
            "2026-11-21T10:41:05+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=5",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_total"], 2)
        self.assertEqual(summary["tool_calls_visible"], 2)
        self.assertEqual(summary["call_id_reuse"], 1)
        self.assertEqual(summary["call_id_reuse_ids"], ["c1"])
        self.assertTrue(any("reused call_id" in warning for warning in summary["warnings"]))

    def test_partial_visibility_nulls_derived_counts(self):
        lines = [
            "2026-11-21T10:42:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:42:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:42:02+08:00 | tool.call | phase=failed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=error; error_code=E1",
            "2026-11-21T10:42:03+08:00 | unknown.event | unknown_kind=unobservable; reason=internal_only; affected_capabilities=[\"H7.4\"]; estimated_count=null",
            "2026-11-21T10:42:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_visibility"], "partial")
        self.assertIsNone(summary["tool_failures"])
        self.assertEqual(summary["observed_tool_failures"], 1)
        self.assertIsNone(summary["permission_blocked"])
        self.assertEqual(summary["observed_permission_blocked"], 0)
        self.assertFalse(summary["derived_counts_complete"])

    def test_h8_subitem_scoring(self):
        lines = [
            "2026-11-21T10:43:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; source=product_log; workspace_pre_hash=sha256:pre",
            "2026-11-21T10:43:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=product_log",
            "2026-11-21T10:43:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; source=product_log",
            "2026-11-21T10:43:03+08:00 | artifact.event | op=create; path=output/extract.csv; content_digest=sha256:b; source=manual",
            "2026-11-21T10:43:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4; source=product_log; workspace_post_hash=sha256:post",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["trace_origin"], "mixed")
        self.assertFalse(summary["h8_scorable"])
        self.assertTrue(summary["h8_subitem_scorable"]["H8.1"])
        self.assertFalse(summary["h8_subitem_scorable"]["H8.3"])

    def test_explicit_trace_origin_cannot_override_observed(self):
        lines = [
            "2026-11-21T10:44:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; source=product_log; trace_origin=product_exported",
            "2026-11-21T10:44:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=product_log",
            "2026-11-21T10:44:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; source=product_log",
            "2026-11-21T10:44:03+08:00 | artifact.event | op=create; path=output/a; content_digest=sha256:a; source=manual",
            "2026-11-21T10:44:04+08:00 | artifact.event | op=create; path=output/b; content_digest=sha256:b; source=manual",
            "2026-11-21T10:44:05+08:00 | artifact.event | op=create; path=output/c; content_digest=sha256:c; source=manual",
            "2026-11-21T10:44:06+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=6; source=product_log",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["trace_origin"], "mixed")
        self.assertTrue(summary["trace_origin_conflict"])
        self.assertFalse(summary["h8_scorable"])
        self.assertTrue(any("trace_origin" in warning for warning in summary["warnings"]))

    def test_dual_source_requested_without_args_merges(self):
        lines = [
            "2026-11-21T10:57:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:57:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=product_log",
            "2026-11-21T10:57:02+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; source=terminal_capture",
            "2026-11-21T10:57:03+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; source=product_log",
            "2026-11-21T10:57:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_total"], 1)
        self.assertEqual(summary["tool_calls_visible"], 1)
        self.assertEqual(summary["tool_calls_incomplete"], 0)
        self.assertEqual(summary["call_id_collisions"], 1)

    def test_permission_counts_with_events_not_nulled(self):
        lines = [
            "2026-11-21T10:50:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=false",
            "2026-11-21T10:50:01+08:00 | permission.event | decision=blocked; scope_type=path; scope_value=C:/secrets; message=outside",
            "2026-11-21T10:50:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_visibility"], "unknown")
        self.assertEqual(summary["permission_blocked"], 1)
        self.assertEqual(summary["observed_permission_blocked"], 1)
        self.assertFalse(summary["permission_counts_complete"])

    def test_single_call_with_mixed_args_representation(self):
        lines = [
            "2026-11-21T10:51:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:51:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            '2026-11-21T10:51:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_summary={"path":"a.txt"}; result_status=success',
            "2026-11-21T10:51:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_total"], 1)
        self.assertEqual(summary["tool_calls_visible"], 1)
        self.assertEqual(summary["tool_calls_incomplete"], 0)
        self.assertEqual(summary["call_id_reuse"], 0)

    def test_h8_2_scorable_without_failures(self):
        lines = [
            "2026-11-21T10:52:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; source=product_log",
            "2026-11-21T10:52:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=product_log",
            "2026-11-21T10:52:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; source=product_log",
            "2026-11-21T10:52:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3; source=product_log",
        ]
        summary = summarize(parse_lines(lines))
        self.assertTrue(summary["h8_subitem_scorable"]["H8.2"])
        self.assertIsNone(summary["h8_subitem_reasons"]["H8.2"])

    def test_h8_2_not_scorable_without_status(self):
        lines = [
            "2026-11-21T10:53:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; source=product_log",
            "2026-11-21T10:53:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=product_log",
            "2026-11-21T10:53:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2; source=product_log",
        ]
        summary = summarize(parse_lines(lines))
        self.assertFalse(summary["h8_subitem_scorable"]["H8.2"])
        self.assertEqual(summary["h8_subitem_reasons"]["H8.2"], "no error/status records")

    def test_args_required_once_per_call(self):
        lines = [
            "2026-11-21T10:54:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:54:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file",
            "2026-11-21T10:54:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:54:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["tool_calls_total"], 1)
        self.assertEqual(summary["duplicate_check_skipped"], 0)

    def test_call_without_args_anywhere_is_rejected(self):
        lines = [
            "2026-11-21T10:55:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:55:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file",
            "2026-11-21T10:55:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; result_status=success",
            "2026-11-21T10:55:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        with self.assertRaises(ParseError):
            parse_lines(lines)

    def test_phase_result_status_consistency(self):
        base = [
            "2026-11-21T10:56:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
        ]
        requested_success = base + [
            "2026-11-21T10:56:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:56:02+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        with self.assertRaises(ParseError):
            parse_lines(requested_success)
        completed_pending = base + [
            "2026-11-21T10:56:03+08:00 | tool.call | phase=completed; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:b; result_status=pending",
            "2026-11-21T10:56:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=2",
        ]
        with self.assertRaises(ParseError):
            parse_lines(completed_pending)


if __name__ == "__main__":
    unittest.main(verbosity=2)
