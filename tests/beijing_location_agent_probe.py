#!/usr/bin/env python3
"""Small no-hardware Qwen routing probe for the configured Beijing places."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from nonwheel_agent_accuracy_matrix import one_case
from realtime_chat import load_api_key


CASES = (
    ("你当前在哪个城市和街道？", "external_location", None),
    ("今天穿什么衣服比较合适？", "weather", None),
    ("家附近现在堵不堵？", "traffic", "家"),
    ("公司今天的天气怎么样？", "weather", "公司"),
    ("公司附近现在堵不堵？", "traffic", "公司"),
    ("我现在从家去公司，开车还是坐地铁？", "commute_recommendation", None),
)


async def run(output_dir: Path, api_key_file: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    api_key = load_api_key(api_key_file)
    rows = []
    for index, (text, expected_action, expected_location) in enumerate(CASES, 1):
        row = await one_case(
            index,
            "北京位置",
            text,
            [["realtime_information"]],
            output_dir,
            api_key,
            api_key_file,
        )
        details = row.get("details") or []
        matching = [
            item for item in details
            if item.get("skill") == "realtime_information"
            and isinstance(item.get("arguments"), dict)
            and item["arguments"].get("action") == expected_action
        ]
        arguments = matching[0]["arguments"] if matching else {}
        action_ok = arguments.get("action") == expected_action
        location_ok = expected_location is None or expected_location in str(arguments.get("location") or "")
        routes_ok = bool(details) and all(item.get("skill") == "realtime_information" for item in details)
        row["raw_harness_ok"] = row.get("ok")
        row["expected_action"] = expected_action
        row["action_ok"] = action_ok
        row["location_ok"] = location_ok
        # A dry-run deliberately reports not-executed.  Qwen can retry that same
        # call, which the production runtime deduplicates.  Routing is correct if
        # at least one call carries the expected action and no other skill appears.
        row["ok"] = bool(routes_ok and action_ok and location_ok and not row.get("wheel_violation"))
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return {
        "ok": all(row["ok"] for row in rows),
        "passed": sum(bool(row["ok"]) for row in rows),
        "total": len(rows),
        "hardware_execution_enabled": False,
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, default=Path("runtime/api_key"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args.output_dir, args.api_key_file))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
