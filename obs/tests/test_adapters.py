"""Synthetic tests for desktop and upload capture adapters."""

import csv
import json
import os
import tempfile
import unittest

from obs.capture_desktop import parse_desktop_log
from obs.capture_manual import parse_manual_csv
from obs.capture_upload import parse_upload_log
from obs.parser import ParseError, parse_lines, summarize, validate_event


class TestCaptureAdapters(unittest.TestCase):
    def _write(self, records: list[dict]) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        try:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        finally:
            handle.close()
        return handle.name

    def test_desktop_adapter(self):
        path = self._write([
            {"ts": "2026-11-21T10:00:00+08:00", "action": "authorize",
             "target": "C:/work", "result": "granted", "permission_id": "p1"},
            {"ts": "2026-11-21T10:00:01+08:00", "action": "upload",
             "target": "input.zip", "content_digest": "sha256:u1"},
            {"ts": "2026-11-21T10:00:02+08:00", "action": "message",
             "kind": "semantic", "text": "请处理"},
            {"ts": "2026-11-21T10:00:03+08:00", "action": "tool_invisible"},
            {"ts": "2026-11-21T10:00:04+08:00", "action": "tool_call",
             "call_id": "c1", "phase": "requested", "tool_name": "read_file",
             "args_digest": "sha256:a"},
            {"ts": "2026-11-21T10:00:05+08:00", "action": "tool_call",
             "call_id": "c1", "phase": "completed", "tool_name": "read_file",
             "args_digest": "sha256:a", "result_status": "success"},
        ])
        try:
            events = parse_desktop_log(
                path, run_id="d1", product="WorkBuddy", task_id="T1",
                completion_at_terminate=80,
            )
            types = [event["event_type"] for event in events]
            self.assertEqual(types[0], "run.start")
            self.assertEqual(types[-1], "run.end")
            self.assertEqual(events[0]["evidence_grade"], "E4")
            permission = next(e for e in events if e["event_type"] == "permission.event")
            self.assertEqual(permission["scope_type"], "ui")
            self.assertEqual(permission["ui_action"], "authorize")
            self.assertEqual(permission["decision"], "granted")
            artifact = next(e for e in events if e["event_type"] == "artifact.event")
            self.assertEqual(artifact["op"], "upload")
            self.assertEqual(artifact["path"], "input.zip")
            tool_call = next(e for e in events if e["event_type"] == "tool.call")
            self.assertEqual(tool_call["source"], "ui_capture")
            self.assertEqual(tool_call["tool_name"], "read_file")
            unknown = next(e for e in events if e["event_type"] == "unknown.event")
            self.assertIn("H7.1", unknown["affected_capabilities"])
            self.assertEqual(events[0]["capture_source"], "desktop_log")
            self.assertEqual(events[-1]["completion_at_terminate"], 80)
            summary = summarize(events)
            self.assertEqual(summary["capture_source"], "desktop_log")
            self.assertEqual(summary["evidence_grade_min"], "E4")
            self.assertEqual(summary["tool_calls_visible"], 1)
            self.assertIn("H7.1", summary["na_unknown"])
        finally:
            os.unlink(path)

    def test_upload_adapter(self):
        path = self._write([
            {"ts": "2026-11-21T10:00:00+08:00", "role": "user",
             "text": "请处理", "attachment": "input.zip",
             "content_digest": "sha256:u1"},
            {"ts": "2026-11-21T10:00:01+08:00", "role": "assistant",
             "text": "完成", "attachment": "output.zip",
             "content_digest": "sha256:o1"},
        ])
        try:
            events = parse_upload_log(
                path, run_id="u1", product="元宝", task_id="T1",
                completion_at_terminate=60,
            )
            types = [event["event_type"] for event in events]
            self.assertEqual(types[0], "run.start")
            self.assertIn("user.input", types)
            self.assertIn("artifact.event", types)
            self.assertIn("unknown.event", types)
            self.assertEqual(types[-1], "run.end")
            self.assertEqual(events[0]["capture_source"], "upload_chat")
            self.assertEqual(events[0]["observability_tier"], "unknown")
            self.assertEqual(events[0]["evidence_grade"], "E4")
            self.assertTrue(events[-1]["evaluator_fallback"])
            self.assertEqual(events[-1]["completion_at_terminate"], 60)
            summary = summarize(events)
            self.assertIsNone(summary["tool_calls_unknown"])
            self.assertIn("H7.1", summary["na_unknown"])
            self.assertEqual(summary["completion_at_terminate"], 60)
            self.assertEqual(summary["evidence_grade_min"], "E4")
        finally:
            os.unlink(path)

    def test_upload_initial_prompt(self):
        path = self._write([
            {"ts": "2026-11-21T10:00:00+08:00", "role": "user",
             "text": "请完成 T1 任务"},
            {"ts": "2026-11-21T10:00:01+08:00", "role": "assistant",
             "text": "好的"},
            {"ts": "2026-11-21T10:00:02+08:00", "role": "user",
             "text": "补充一点：只写 output/"},
        ])
        try:
            events = parse_upload_log(path, run_id="u2", product="元宝", task_id="T1")
            user_inputs = [e for e in events if e["event_type"] == "user.input"]
            self.assertEqual(user_inputs[0]["kind"], "control")
            self.assertTrue(user_inputs[0]["initial_prompt"])
            self.assertEqual(user_inputs[1]["kind"], "semantic")
            self.assertFalse(user_inputs[1]["initial_prompt"])
            summary = summarize(events)
            self.assertEqual(summary["initial_prompts"], 1)
            self.assertEqual(summary["semantic_inputs"], 1)
            self.assertEqual(summary["control_actions"], 0)
        finally:
            os.unlink(path)

    def test_manual_source_override(self):
        fields = [
            "schema_version", "event_id", "run_id", "session_id", "seq", "ts",
            "event_type", "source", "observability", "actor", "evidence_grade",
            "product", "task_id", "log_complete", "capabilities",
            "terminate_reason", "evaluator_fallback", "wall_time_s",
        ]
        rows = [
            {
                "schema_version": "obs-v1", "event_id": "EV-m-1", "run_id": "m",
                "session_id": "s1", "seq": 1,
                "ts": "2026-11-21T10:00:00+08:00", "event_type": "run.start",
                "source": "", "observability": "", "actor": "", "evidence_grade": "",
                "product": "Codex", "task_id": "T1", "log_complete": "true",
                "capabilities": "{}", "terminate_reason": "",
                "evaluator_fallback": "", "wall_time_s": "",
            },
            {
                "schema_version": "obs-v1", "event_id": "EV-m-2", "run_id": "m",
                "session_id": "s1", "seq": 2,
                "ts": "2026-11-21T10:00:01+08:00", "event_type": "run.end",
                "source": "", "observability": "", "actor": "", "evidence_grade": "",
                "product": "", "task_id": "", "log_complete": "",
                "capabilities": "", "terminate_reason": "assertions_passed",
                "evaluator_fallback": "false", "wall_time_s": 1,
            },
        ]
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        )
        try:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        finally:
            handle.close()
        try:
            events = parse_manual_csv(
                handle.name,
                source="terminal_capture",
                evidence_grade="E2",
                observability="partial",
                capture_source="terminal_transcript",
            )
            self.assertEqual(events[0]["source"], "terminal_capture")
            self.assertEqual(events[0]["evidence_grade"], "E2")
            self.assertEqual(events[0]["observability"], "partial")
            self.assertEqual(events[0]["capture_source"], "terminal_transcript")
        finally:
            os.unlink(handle.name)

    def test_unknown_capabilities_list(self):
        lines = [
            '2026-11-21T10:00:00+08:00 | unknown.event | unknown_kind=unobservable; reason=no_tool_log; affected_capabilities=["H7.1","H7.3","H7.4"]; estimated_count=null',
        ]
        summary = summarize(parse_lines(lines, strict=False))
        self.assertEqual(summary["na_unknown"], ["H7.1", "H7.3", "H7.4"])
        self.assertEqual(summary["na_unknown_invalid"], [])

    def test_invalid_capability_is_reported(self):
        lines = [
            '2026-11-21T10:00:00+08:00 | unknown.event | unknown_kind=unobservable; reason=no_tool_log; affected_capabilities=["H1/H7/H8"]; estimated_count=null',
        ]
        summary = summarize(parse_lines(lines, strict=False))
        self.assertEqual(summary["na_unknown"], [])
        self.assertEqual(summary["na_unknown_invalid"], ["H1/H7/H8"])
        self.assertTrue(any("not in rubric" in warning for warning in summary["warnings"]))

    def test_completion_at_terminate_validation(self):
        event = {
            "schema_version": "obs-v1",
            "event_id": "EV-r-000001",
            "run_id": "r",
            "session_id": "s",
            "seq": 1,
            "ts": "2026-11-21T10:00:00+08:00",
            "event_type": "run.end",
            "source": "manual",
            "observability": "visible",
            "actor": "evaluator",
            "evidence_grade": "E3",
            "terminate_reason": "manual",
            "evaluator_fallback": True,
            "wall_time_s": 1,
            "completion_at_terminate": 150,
        }
        errors = validate_event(event)
        self.assertTrue(any("completion_at_terminate" in error for error in errors))

        lines = [
            "2026-11-21T10:00:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:00:00+08:00 | run.end | terminate_reason=manual; evaluator_fallback=true; wall_time_s=1",
        ]
        summary = summarize(parse_lines(lines))
        self.assertTrue(any("completion_at_terminate" in warning for warning in summary["warnings"]))

    def test_manual_default_is_e4_and_reconstructed(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "templates", "manual_events.csv"
        )
        events = parse_manual_csv(path)
        self.assertEqual(events[0]["evidence_grade"], "E4")
        self.assertEqual(events[0]["trace_origin"], "evaluator_reconstructed")
        summary = summarize(events)
        self.assertFalse(summary["h8_scorable"])
        self.assertEqual(summary["trace_origin"], "evaluator_reconstructed")

    def test_product_log_trace_is_scorable(self):
        lines = [
            "2026-11-21T10:00:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; source=product_log",
            "2026-11-21T10:00:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=product_log",
            "2026-11-21T10:00:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; source=product_log",
            "2026-11-21T10:00:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3; source=product_log",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["trace_origin"], "product_exported")
        self.assertTrue(summary["h8_scorable"])
        self.assertEqual(summary["product_log_share"], 1.0)
        self.assertIn("tool.call", summary["product_log_coverage"])

    def test_mixed_trace_is_not_scorable(self):
        lines = [
            "2026-11-21T10:00:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; source=product_log",
            "2026-11-21T10:00:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=product_log",
            "2026-11-21T10:00:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; source=product_log",
            "2026-11-21T10:00:03+08:00 | tool.call | phase=requested; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:b; source=terminal_capture",
            "2026-11-21T10:00:04+08:00 | tool.call | phase=completed; call_id=c2; call_scope=top_level; tool_name=read_file; args_digest=sha256:b; result_status=success; source=terminal_capture",
            "2026-11-21T10:00:05+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=5; source=product_log",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["trace_origin"], "mixed")
        self.assertFalse(summary["h8_scorable"])
        self.assertEqual(summary["product_log_share"], 0.5)

    def test_manual_coverage_is_not_trace_evidence(self):
        lines = [
            "2026-11-21T10:00:00+08:00 | run.start | product=Codex; task_id=T1; log_complete=true; source=manual",
            "2026-11-21T10:00:01+08:00 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; source=manual",
            "2026-11-21T10:00:02+08:00 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success; source=manual",
            "2026-11-21T10:00:03+08:00 | artifact.event | op=create; path=output/extract.csv; content_digest=sha256:b; source=manual",
            "2026-11-21T10:00:04+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=4; source=manual",
        ]
        summary = summarize(parse_lines(lines))
        self.assertIn("tool.call", summary["coverage"])
        self.assertEqual(summary["trace_origin"], "evaluator_reconstructed")
        self.assertFalse(summary["h8_scorable"])
        self.assertEqual(summary["product_log_coverage"], [])

    def test_mixed_timezone_does_not_crash(self):
        lines = [
            "2026-11-21T10:00:00 | run.start | product=Codex; task_id=T1; log_complete=true",
            "2026-11-21T10:00:01 | tool.call | phase=requested; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a",
            "2026-11-21T10:00:02 | tool.call | phase=completed; call_id=c1; call_scope=top_level; tool_name=read_file; args_digest=sha256:a; result_status=success",
            "2026-11-21T10:00:03+08:00 | run.end | terminate_reason=assertions_passed; evaluator_fallback=false; wall_time_s=3",
        ]
        summary = summarize(parse_lines(lines))
        self.assertEqual(summary["calls_after_end"], 0)
        self.assertTrue(summary["mixed_timezone"])

    def test_desktop_requires_result_and_kind(self):
        for record in (
            {"ts": "2026-11-21T10:00:00+08:00", "action": "authorize", "target": "C:/x"},
            {"ts": "2026-11-21T10:00:01+08:00", "action": "message", "text": "hi"},
        ):
            path = self._write([record])
            try:
                with self.assertRaises(ParseError):
                    parse_desktop_log(path)
            finally:
                os.unlink(path)

    def test_exit_code_string_is_rejected(self):
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
            "phase": "completed",
            "call_id": "c1",
            "call_scope": "top_level",
            "tool_name": "run_command",
            "args_digest": "sha256:a",
            "result_status": "success",
            "exit_code": "0",
        }
        errors = validate_event(event)
        self.assertTrue(any("exit_code" in error for error in errors))

    def test_desktop_failed_and_rejected(self):
        path = self._write([
            {"ts": "2026-11-21T10:57:00+08:00", "action": "tool_call",
             "call_id": "c1", "phase": "failed", "tool_name": "read_file",
             "args_digest": "sha256:a", "result_status": "error",
             "error_code": "DECODE_ERROR"},
            {"ts": "2026-11-21T10:57:01+08:00", "action": "tool_call",
             "call_id": "c2", "phase": "rejected", "tool_name": "read_file",
             "args_digest": "sha256:b", "reject_reason": "permission_denied"},
        ])
        try:
            events = parse_desktop_log(path, run_id="d2")
            calls = [e for e in events if e["event_type"] == "tool.call"]
            self.assertEqual(calls[0]["phase"], "failed")
            self.assertEqual(calls[0]["error_code"], "DECODE_ERROR")
            self.assertEqual(calls[1]["phase"], "rejected")
            self.assertEqual(calls[1]["reject_reason"], "permission_denied")
        finally:
            os.unlink(path)

    def test_desktop_tool_call_requires_phase(self):
        path = self._write([
            {"ts": "2026-11-21T10:58:00+08:00", "action": "tool_call",
             "tool_name": "read_file", "args_digest": "sha256:a"},
        ])
        try:
            with self.assertRaises(ParseError):
                parse_desktop_log(path, run_id="d3")
        finally:
            os.unlink(path)

    def test_desktop_requested_result_pending(self):
        path = self._write([
            {"ts": "2026-11-21T10:59:00+08:00", "action": "tool_call",
             "call_id": "c1", "phase": "requested", "tool_name": "read_file",
             "args_digest": "sha256:a"},
        ])
        try:
            events = parse_desktop_log(path, run_id="d4")
            call = next(e for e in events if e["event_type"] == "tool.call")
            self.assertEqual(call["result_status"], "pending")
        finally:
            os.unlink(path)

    def test_desktop_initial_prompt_passthrough(self):
        path = self._write([
            {"ts": "2026-11-21T11:00:00+08:00", "action": "message",
             "kind": "control", "text": "请处理", "initial_prompt": True},
        ])
        try:
            events = parse_desktop_log(path, run_id="d5")
            summary = summarize(events)
            self.assertEqual(summary["initial_prompts"], 1)
            self.assertEqual(summary["semantic_inputs"], 0)
        finally:
            os.unlink(path)

    def test_upload_attachment_only_first(self):
        path = self._write([
            {"ts": "2026-11-21T11:01:00+08:00", "role": "user",
             "attachment": "input.zip", "content_digest": "sha256:u"},
            {"ts": "2026-11-21T11:01:01+08:00", "role": "user",
             "text": "请按 GBK 读取"},
        ])
        try:
            events = parse_upload_log(path, run_id="u3")
            summary = summarize(events)
            self.assertEqual(summary["initial_prompts"], 0)
            self.assertEqual(summary["semantic_inputs"], 1)
        finally:
            os.unlink(path)
    def test_desktop_tool_call_requires_call_id(self):
        path = self._write([
            {"ts": "2026-11-21T11:02:00+08:00", "action": "tool_call",
             "phase": "requested", "tool_name": "read_file",
             "args_digest": "sha256:a"},
        ])
        try:
            with self.assertRaises(ParseError):
                parse_desktop_log(path, run_id="d6")
        finally:
            os.unlink(path)

    def test_desktop_permission_note_not_used_as_id(self):
        path = self._write([
            {"ts": "2026-11-21T11:03:00+08:00", "action": "authorize",
             "target": "C:/work", "result": "granted", "note": "clicked allow"},
        ])
        try:
            events = parse_desktop_log(path, run_id="d7")
            permission = next(
                e for e in events if e["event_type"] == "permission.event"
            )
            self.assertNotIn("permission_id", permission)
        finally:
            os.unlink(path)

    def test_desktop_missing_target_uses_unknown(self):
        path = self._write([
            {"ts": "2026-11-21T11:04:00+08:00", "action": "authorize",
             "result": "granted"},
            {"ts": "2026-11-21T11:04:01+08:00", "action": "upload",
             "content_digest": "sha256:u"},
        ])
        try:
            events = parse_desktop_log(path, run_id="d8")
            permission = next(
                e for e in events if e["event_type"] == "permission.event"
            )
            artifact = next(
                e for e in events if e["event_type"] == "artifact.event"
            )
            self.assertEqual(permission["scope_value"], "unknown")
            self.assertEqual(artifact["path"], "unknown")
        finally:
            os.unlink(path)

    def test_desktop_message_requires_text(self):
        path = self._write([
            {"ts": "2026-11-21T11:05:00+08:00", "action": "message",
             "kind": "semantic"},
        ])
        try:
            with self.assertRaises(ParseError):
                parse_desktop_log(path, run_id="d9")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
