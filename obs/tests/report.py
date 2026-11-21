"""Print a synthetic test report for obs-v1 parser. No real product data."""

import json
import sys

from obs.parser import parse_lines, summarize
from obs.tests.test_parser import SCENARIOS


def compare(expected: dict, actual: dict) -> list[tuple]:
    diffs = []
    for key, value in expected.items():
        if actual.get(key) != value:
            diffs.append((key, value, actual.get(key)))
    return diffs


def main() -> None:
    passed = 0
    for scenario in SCENARIOS:
        events = parse_lines(scenario["lines"], run_id="synthetic")
        summary = summarize(events, scenario["manifest"])
        diffs = compare(scenario["expect"], summary)
        status = "PASS" if not diffs else "FAIL"
        if not diffs:
            passed += 1

        print(f"\n=== {scenario['name']} [{status}] ===")
        print("--- 输入样例 ---")
        print("\n".join(scenario["lines"]))
        print("--- 解析后事件 ---")
        print(json.dumps(events, ensure_ascii=False, indent=2))
        print("--- 预期统计 ---")
        print(json.dumps(scenario["expect"], ensure_ascii=False, indent=2))
        print("--- 实际统计 ---")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if diffs:
            print("--- 差异 ---")
            for key, expected, actual in diffs:
                print(f"{key}: expected={expected!r} actual={actual!r}")

    print(f"\n=== 汇总 ===\n{passed}/{len(SCENARIOS)} passed")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
