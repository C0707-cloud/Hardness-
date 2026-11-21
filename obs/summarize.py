"""Summary writers for obs-v1."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from obs.parser import summarize

__all__ = ["summarize", "write_summary_json", "write_summary_csv"]


def write_summary_json(path: str | Path, summary: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_summary_csv(path: str | Path, summary: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["key", "value"])
        for key, value in summary.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, bool):
                value = "true" if value else "false"
            elif value is None:
                value = ""
            writer.writerow([key, value])
