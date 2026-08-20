#!/usr/bin/env python3
"""Cloud semantic-routing regression matrix; never opens or executes hardware."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realtime_chat import JsonLogger, RealtimeConversation, load_api_key, parser as runtime_parser


CASES = [
    ("关闭会议，投影导航到客厅去", ["scenario:meeting_projection_stop", "navigation_goto"]),
    ("把会议画面收起来，再去客厅", ["scenario:meeting_projection_stop", "navigation_goto"]),
    ("投影先停掉，接着回客厅", ["scenario:meeting_projection_stop", "navigation_goto"]),
    ("我不想开灯，导航去客厅", ["navigation_goto"]),
    ("先去客厅，再把灯打开", ["navigation_goto", "light_control"]),
    ("你知道理想公司吗", []),
    ("理想汽车公司在哪里", []),
    ("机器人现在在哪里", ["realtime_information"]),
    ("能介绍一下会议投影功能吗", []),
    ("我要开会了", ["scenario:meeting_projection"]),
    ("关掉会议投影，但别移动", ["scenario:meeting_projection_stop"]),
    ("去书房，不要投影", ["navigation_goto"]),
    ("先导航到客厅，再启动投食器", ["navigation_goto", "feeder_control"]),
    ("提醒我下午三点开会", ["reminder_schedule"]),
    ("先关投影，然后给豆豆喂十克", ["scenario:meeting_projection_stop", "feeder_control"]),
    ("客厅有什么", []),
    ("我想休息一会", ["scenario:rest_lighting"]),
    ("先停止音乐，再查一下天气", ["media_player", "realtime_information"]),
    ("把灯关了，然后播放音乐", ["light_control", "media_player"]),
    ("先看看机器人在哪，再导航到书房", ["realtime_information", "navigation_goto"]),
]


def signatures(result: dict) -> list[str]:
    values: list[str] = []
    for tool in result.get("tool_results") or []:
        if tool.get("skill") == "run_skill_sequence":
            children = tool.get("tasks") or (tool.get("structured_result") or {}).get("tasks") or []
            for child in children:
                name = str(child.get("name") or "")
                arguments = child.get("arguments") or {}
                if name == "run_robot_scenario":
                    values.append(f"scenario:{arguments.get('scenario', '')}")
                else:
                    values.append(name)
        elif tool.get("skill") == "run_robot_scenario":
            values.append(f"scenario:{tool.get('scenario', '')}")
        else:
            values.append(str(tool.get("skill") or ""))
    return values


async def run(output_dir: Path, api_key_file: Path) -> dict:
    args = runtime_parser().parse_args(
        ["--tool-test-text", "matrix", "--skill-backend", "subprocess", "--no-reconnect"]
    )
    args.api_key_file = api_key_file
    args.memory_dir = output_dir / "memory"
    output_dir.mkdir(parents=True, exist_ok=True)
    client = RealtimeConversation(
        args,
        load_api_key(api_key_file),
        JsonLogger(output_dir / "events.jsonl"),
    )
    results = []
    try:
        await client.connect()
        for index, (text, expected) in enumerate(CASES, 1):
            args.tool_test_expect_tool = bool(expected)
            error = ""
            try:
                await client.run_tool_test(text, output_dir / f"case_{index:02d}.wav", 60)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
            result = dict(client.last_tool_test_result or {})
            actual = signatures(result)
            results.append(
                {
                    "index": index,
                    "text": text,
                    "expected": expected,
                    "actual": actual,
                    "ok": bool(result.get("ok") and actual == expected and not error),
                    "error": error,
                    "transcripts": result.get("transcripts") or [],
                }
            )
        return {
            "ok": all(item["ok"] for item in results),
            "passed": sum(item["ok"] for item in results),
            "total": len(results),
            "results": results,
            "microphone_opened": False,
            "speaker_opened": False,
            "hardware_opened": False,
        }
    finally:
        if client.websocket is not None:
            with contextlib.suppress(Exception):
                await client.websocket.close()


def main() -> int:
    cli = argparse.ArgumentParser()
    cli.add_argument("--output-dir", type=Path, required=True)
    cli.add_argument("--api-key-file", type=Path, default=Path("runtime/api_key"))
    cli.add_argument("--report", type=Path, required=True)
    values = cli.parse_args()
    report = asyncio.run(run(values.output_dir, values.api_key_file))
    values.report.parent.mkdir(parents=True, exist_ok=True)
    values.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
