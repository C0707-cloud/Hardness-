"""Synthetic tests for obs-v1 I/O, capture, summary, and CLI."""

import json
import os
import tempfile
import unittest

from obs.capture_manual import parse_manual_csv
from obs.capture_terminal import import_raw_transcript, parse_tagged_file
from obs.cli import main as cli_main
from obs.io_csv import read_csv, write_csv
from obs.io_jsonl import read_jsonl, write_jsonl
from obs.parser import parse_lines, summarize
from obs.summarize import write_summary_csv, write_summary_json
from obs.tests.test_parser import SCENARIOS


class TestObsIO(unittest.TestCase):
    def test_jsonl_csv_roundtrip(self):
        events = parse_lines(SCENARIOS[0]["lines"])
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = os.path.join(tmp, "events.jsonl")
            csv_path = os.path.join(tmp, "events.csv")
            write_jsonl(jsonl_path, events)
            back = read_jsonl(jsonl_path)
            self.assertEqual(len(back), len(events))
            write_csv(csv_path, back)
            csv_events = read_csv(csv_path)
            self.assertEqual(len(csv_events), len(events))
            for original, restored in zip(back, csv_events):
                for key in ("event_type", "call_id", "tool_name", "phase", "result_status", "path"):
                    if key in original:
                        self.assertEqual(original.get(key), restored.get(key))

    def test_manual_csv(self):
        content = (
            "schema_version,event_id,run_id,session_id,seq,ts,event_type,source,"
            "observability,actor,evidence_grade,product,task_id,log_complete,"
            "capabilities,terminate_reason,evaluator_fallback,wall_time_s\n"
            "obs-v1,EV-m-1,m,s1,1,2026-11-21T10:00:00+08:00,run.start,manual,"
            "visible,evaluator,E3,Codex,T1,true,{},,,\n"
            "obs-v1,EV-m-2,m,s1,2,2026-11-21T10:00:01+08:00,run.end,manual,"
            "visible,evaluator,E3,,,,,assertions_passed,false,1\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as handle:
            handle.write(content)
            path = handle.name
        try:
            events = parse_manual_csv(path)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["event_type"], "run.start")
            self.assertEqual(events[1]["terminate_reason"], "assertions_passed")
        finally:
            os.unlink(path)

    def test_tagged_terminal_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write("\n".join(SCENARIOS[0]["lines"]))
            path = handle.name
        try:
            events = parse_tagged_file(path)
            self.assertEqual(len(events), 5)
            self.assertEqual(events[0]["event_type"], "run.start")
        finally:
            os.unlink(path)

    def test_raw_transcript_import(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write("raw terminal output without structure\n")
            path = handle.name
        try:
            events = import_raw_transcript(path, run_id="r1", ts="2026-11-21T10:00:00+08:00")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["evidence.gap", "unknown.event"],
            )
            summary = summarize(events)
            self.assertIsNone(summary["tool_calls_unknown"])
            self.assertEqual(summary["evidence_gaps"], ["no_raw_structure"])
            self.assertEqual(events[0]["ts"], "2026-11-21T10:00:00+08:00")
            self.assertEqual(len(events[0]["raw_sha256"]), 64)
            self.assertEqual(events[0]["raw_sha256"], events[1]["raw_sha256"])
        finally:
            os.unlink(path)

    def test_summary_writers(self):
        events = parse_lines(SCENARIOS[0]["lines"])
        summary = summarize(events, {"log_complete": True})
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "summary.json")
            csv_path = os.path.join(tmp, "summary.csv")
            write_summary_json(json_path, summary)
            write_summary_csv(csv_path, summary)
            with open(json_path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            self.assertEqual(loaded["tool_calls_visible"], 1)
            with open(csv_path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("tool_calls_visible,1", text)

    def test_cli_validate(self):
        events = parse_lines(SCENARIOS[0]["lines"])
        with tempfile.TemporaryDirectory() as tmp:
            good_path = os.path.join(tmp, "good.jsonl")
            write_jsonl(good_path, events)
            self.assertEqual(cli_main(["validate", "--events", good_path]), 0)
            bad_path = os.path.join(tmp, "bad.jsonl")
            with open(bad_path, "w", encoding="utf-8") as handle:
                handle.write('{"event_type":"tool.call"}\n')
            self.assertEqual(cli_main(["validate", "--events", bad_path]), 1)

    def test_cli_convert_and_summarize(self):
        with tempfile.TemporaryDirectory() as tmp:
            terminal_path = os.path.join(tmp, "terminal.txt")
            with open(terminal_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(SCENARIOS[0]["lines"]))
            events_path = os.path.join(tmp, "events.jsonl")
            self.assertEqual(
                cli_main([
                    "convert", "--from", "terminal_tagged",
                    "--input", terminal_path, "--output", events_path,
                ]),
                0,
            )
            summary_path = os.path.join(tmp, "summary.json")
            self.assertEqual(
                cli_main([
                    "summarize", "--events", events_path, "--out", summary_path,
                ]),
                0,
            )
            with open(summary_path, encoding="utf-8") as handle:
                summary = json.load(handle)
            self.assertEqual(summary["tool_calls_visible"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
