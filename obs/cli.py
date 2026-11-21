"""Command line interface for obs-v1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from obs.capture_desktop import parse_desktop_log
from obs.capture_manual import parse_manual_csv
from obs.capture_terminal import import_raw_transcript, parse_tagged_file
from obs.capture_upload import parse_upload_log
from obs.io_csv import read_csv, write_csv
from obs.io_jsonl import read_jsonl, write_jsonl
from obs.parser import ParseError, SOURCES, summarize, validate_events
from obs.scoring import score_run, write_scores
from obs.summarize import write_summary_csv, write_summary_json
from obs.workspace import (
    WorkspaceError,
    collect_workspace,
    diff_snapshots,
    load_snapshot,
    write_diff,
    write_snapshot,
)


def _read_events(path: str, fmt: str) -> list[dict]:
    if fmt == "jsonl":
        return read_jsonl(path)
    if fmt == "csv":
        return read_csv(path)
    raise ParseError(f"unsupported event format: {fmt}")


def _print_report(report: dict) -> int:
    for error in report["errors"]:
        print(f"ERROR: {error}", file=sys.stderr)
    for warning in report["warnings"]:
        print(f"WARN: {warning}", file=sys.stderr)
    return 1 if report["errors"] else 0


def _cmd_validate(args) -> int:
    try:
        events = _read_events(args.events, args.format)
    except ParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    report = validate_events(events)
    code = _print_report(report)
    if code == 0:
        print(f"OK: {len(events)} event(s) valid")
    return code


def _cmd_convert(args) -> int:
    try:
        if args.from_format == "terminal_tagged":
            events = parse_tagged_file(args.input, run_id=args.run_id)
        elif args.from_format == "terminal_raw":
            events = import_raw_transcript(args.input, run_id=args.run_id)
        elif args.from_format == "desktop_log":
            events = parse_desktop_log(
                args.input,
                run_id=args.run_id,
                product=args.product or "desktop",
                task_id=args.task_id,
                evidence_grade=args.evidence_grade or "E4",
                terminate_reason=args.terminate_reason,
                evaluator_fallback=False if args.evaluator_fallback is None else args.evaluator_fallback,
                completion_at_terminate=args.completion_at_terminate,
            )
        elif args.from_format == "upload_chat":
            events = parse_upload_log(
                args.input,
                run_id=args.run_id,
                product=args.product or "upload",
                task_id=args.task_id,
                evidence_grade=args.evidence_grade or "E4",
                terminate_reason=args.terminate_reason,
                evaluator_fallback=True if args.evaluator_fallback is None else args.evaluator_fallback,
                completion_at_terminate=args.completion_at_terminate,
            )
        elif args.from_format == "manual_csv":
            events = parse_manual_csv(
                args.input,
                run_id=args.run_id,
                source=args.source,
                evidence_grade=args.evidence_grade,
                observability=args.observability,
                capture_source=args.capture_source,
            )
        else:
            events = _read_events(args.input, args.from_format)
    except ParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = validate_events(events)
    if report["errors"]:
        return _print_report(report)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    if args.to_format == "jsonl":
        write_jsonl(args.output, events)
    else:
        write_csv(args.output, events)
    print(f"OK: wrote {len(events)} event(s) to {args.output}")
    return 0


def _cmd_summarize(args) -> int:
    try:
        events = _read_events(args.events, args.format)
    except ParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = validate_events(events)
    if report["errors"]:
        return _print_report(report)
    for warning in report["warnings"]:
        print(f"WARN: {warning}", file=sys.stderr)

    manifest = {}
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as handle:
            manifest = json.load(handle)

    summary = summarize(events, manifest)
    write_summary_json(args.out, summary)
    if args.csv_out:
        write_summary_csv(args.csv_out, summary)
    print(f"OK: summary written to {args.out}")
    return 0


def _cmd_snapshot(args) -> int:
    snapshot = collect_workspace(
        args.workspace,
        kind=args.kind,
        run_id=args.run_id,
        ignore_dirs=args.ignore_dir,
        evidence_dirs=args.evidence_dir,
        ignore_patterns=args.ignore_pattern,
        exclude_paths=list(args.exclude) + [args.out],
    )
    write_snapshot(args.out, snapshot)
    if not snapshot["root_exists"]:
        print(
            f"WARN: workspace does not exist: {snapshot['workspace']}",
            file=sys.stderr,
        )
    for error in snapshot["errors"]:
        print(f"WARN: {error['path']}: {error['error']}", file=sys.stderr)
    print(
        f"OK: {len(snapshot['entries'])} file(s), "
        f"{len(snapshot['errors'])} error(s) -> {args.out}"
    )
    return 1 if args.strict and snapshot["errors"] else 0


def _cmd_workspace_diff(args) -> int:
    pre = load_snapshot(args.pre)
    post = load_snapshot(args.post)
    diff = diff_snapshots(pre, post)
    write_diff(args.out, diff)
    summary = diff["summary"]
    print(
        f"OK: +{summary['added']} ~{summary['modified']} "
        f"-{summary['deleted']} ={summary['unchanged']} "
        f"!{summary['errors']} -> {args.out}"
    )
    return 0


def _read_json_optional(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _cmd_score(args) -> int:
    events = _read_events(args.events, args.format)
    verdict = _read_json_optional(args.verdict)
    workspace_diff = _read_json_optional(args.workspace_diff)
    run_record = _read_json_optional(args.run_record)
    scores = score_run(events, verdict, workspace_diff, run_record)
    write_scores(args.out, scores)
    total = scores["total"]
    print(
        f"OK: {total['score']}/{total['max']} "
        f"(coverage {total['coverage']}) -> {args.out}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    validate = sub.add_parser("validate", help="validate events")
    validate.add_argument("--events", required=True)
    validate.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    validate.set_defaults(func=_cmd_validate)

    convert = sub.add_parser("convert", help="convert a source into events")
    convert.add_argument(
        "--from",
        dest="from_format",
        required=True,
        choices=[
            "terminal_tagged",
            "terminal_raw",
            "desktop_log",
            "upload_chat",
            "manual_csv",
            "jsonl",
            "csv",
        ],
    )
    convert.add_argument("--input", required=True)
    convert.add_argument("--output", required=True)
    convert.add_argument("--to", dest="to_format", choices=["jsonl", "csv"], default="jsonl")
    convert.add_argument("--run-id", default="synthetic")
    convert.add_argument("--product", default=None)
    convert.add_argument("--task-id", default="T1")
    convert.add_argument("--source", choices=sorted(SOURCES), default=None)
    convert.add_argument("--evidence-grade", choices=["E1", "E2", "E3", "E4"], default=None)
    convert.add_argument("--observability", choices=["visible", "partial", "unknown"], default=None)
    convert.add_argument("--capture-source", default=None)
    convert.add_argument("--completion-at-terminate", type=float, default=None)
    convert.add_argument("--terminate-reason", default="assertions_passed")
    convert.add_argument(
        "--evaluator-fallback",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    convert.set_defaults(func=_cmd_convert)

    summarize_cmd = sub.add_parser("summarize", help="summarize one run")
    summarize_cmd.add_argument("--events", required=True)
    summarize_cmd.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    summarize_cmd.add_argument("--manifest", default=None)
    summarize_cmd.add_argument("--out", required=True)
    summarize_cmd.add_argument("--csv", dest="csv_out", default=None)
    summarize_cmd.set_defaults(func=_cmd_summarize)

    snapshot_cmd = sub.add_parser("snapshot", help="collect a workspace pre/post snapshot")
    snapshot_cmd.add_argument("--workspace", required=True)
    snapshot_cmd.add_argument("--out", required=True)
    snapshot_cmd.add_argument("--kind", choices=["pre", "post"], required=True)
    snapshot_cmd.add_argument("--run-id", default="run")
    snapshot_cmd.add_argument("--ignore-dir", action="append", default=[])
    snapshot_cmd.add_argument("--evidence-dir", action="append", default=None)
    snapshot_cmd.add_argument("--ignore-pattern", action="append", default=[])
    snapshot_cmd.add_argument("--exclude", action="append", default=[])
    snapshot_cmd.add_argument("--strict", action="store_true")
    snapshot_cmd.set_defaults(func=_cmd_snapshot)

    diff_cmd = sub.add_parser("workspace-diff", help="diff workspace pre/post snapshots")
    diff_cmd.add_argument("--pre", required=True)
    diff_cmd.add_argument("--post", required=True)
    diff_cmd.add_argument("--out", required=True)
    diff_cmd.set_defaults(func=_cmd_workspace_diff)

    score_cmd = sub.add_parser("score", help="score one run with rubric-v1.0")
    score_cmd.add_argument("--events", required=True)
    score_cmd.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    score_cmd.add_argument("--verdict", default=None)
    score_cmd.add_argument("--workspace-diff", dest="workspace_diff", default=None)
    score_cmd.add_argument("--run-record", dest="run_record", default=None)
    score_cmd.add_argument("--out", required=True)
    score_cmd.set_defaults(func=_cmd_score)

    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ParseError, WorkspaceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
