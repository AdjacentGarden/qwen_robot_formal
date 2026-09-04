#!/usr/bin/env python3
"""Cloud semantic matrix for parameterized scenes; dry-run, no devices."""

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
    ("我要开会了", ["scenario:meeting_projection", "step:navigation_goto:goto:study_projection", "step:head_control:up", "step:projector_control:meeting_presentation_on"]),
    ("就在原地开始会议投影", ["scenario:meeting_projection", "step:head_control:up", "step:projector_control:meeting_presentation_on"]),
    ("原地播放会议内容，不要抬头", ["scenario:meeting_projection", "step:projector_control:meeting_presentation_on"]),
    ("在客厅播放会议内容", ["scenario:meeting_projection", "step:navigation_goto:goto:white_wall", "step:head_control:up", "step:projector_control:meeting_presentation_on"]),
    ("暂停会议画面", ["projector_control:meeting_pause"]),
    ("继续会议画面", ["projector_control:meeting_resume"]),
    ("结束会议投影", ["scenario:meeting_projection_stop", "step:projector_control:off", "step:head_control:level"]),
    ("打开投影仪", ["projector_control:on"]),
    ("给豆豆喂十克", ["feeder_control:feed:grams=10"]),
    ("启动两份投食", ["feeder_control:feed:portions=2"]),
    ("不要去找豆豆，只投食十克", ["feeder_control:feed:grams=10"]),
    ("帮我看看豆豆在干嘛，他该吃饭了", ["scenario:find_and_feed_doudou", "step:navigation_goto:goto:white_wall", "step:pet_tracking:find", "step:navigation_goto:goto:study_projection", "step:pet_tracking:find", "step:navigation_goto:goto:origin", "step:pet_tracking:find", "step:feeder_control:feed"]),
    ("去书房找一下豆豆", ["scenario:find_pet_at", "step:navigation_goto:goto:study_projection", "step:pet_tracking:find"]),
    ("先给豆豆投食两份，然后打开投影仪", ["feeder_control:feed:portions=2", "projector_control:on"]),
    ("先打开投影仪，然后暂停会议画面", ["projector_control:on", "projector_control:meeting_pause"]),
]


def atomic_signature(name: str, arguments: dict) -> str:
    action = str(arguments.get("action") or "")
    value = f"{name}:{action}" if action else name
    if "grams" in arguments:
        value += f":grams={arguments['grams']}"
    if "portions" in arguments:
        value += f":portions={arguments['portions']}"
    return value


def signatures(result: dict) -> list[str]:
    values: list[str] = []
    for tool in result.get("tool_results") or []:
        skill = str(tool.get("skill") or "")
        if skill == "run_skill_sequence":
            children = tool.get("tasks") or (tool.get("structured_result") or {}).get("tasks") or []
            for child in children:
                name = str(child.get("name") or "")
                arguments = dict(child.get("arguments") or {})
                if name == "run_robot_scenario":
                    values.append(f"scenario:{arguments.get('scenario', '')}")
                    result = dict(child.get("result") or {})
                    for step in result.get("steps") or []:
                        if step.get("skipped"):
                            continue
                        step_value = f"step:{step.get('skill', '')}:{step.get('action', '')}"
                        step_arguments = ((step.get("result") or {}).get("arguments") or {})
                        if step.get("skill") == "navigation_goto" and step_arguments.get("point"):
                            step_value += f":{step_arguments['point']}"
                        values.append(step_value)
                else:
                    values.append(atomic_signature(name, arguments))
            continue
        if skill == "run_robot_scenario":
            values.append(f"scenario:{tool.get('scenario', '')}")
            for step in tool.get("steps") or []:
                if step.get("skipped"):
                    continue
                step_value = f"step:{step.get('skill', '')}:{step.get('action', '')}"
                arguments = ((step.get("result") or {}).get("arguments") or {})
                if step.get("skill") == "navigation_goto" and arguments.get("point"):
                    step_value += f":{arguments['point']}"
                values.append(step_value)
            continue
        values.append(atomic_signature(skill, dict(tool.get("arguments") or {})))
    return values


async def run(output_dir: Path, api_key_file: Path) -> dict:
    args = runtime_parser().parse_args(
        ["--tool-test-text", "parameterized-matrix", "--skill-backend", "subprocess", "--no-reconnect"]
    )
    args.api_key_file = api_key_file
    args.memory_dir = output_dir / "memory"
    output_dir.mkdir(parents=True, exist_ok=True)
    client = RealtimeConversation(args, load_api_key(api_key_file), JsonLogger(output_dir / "events.jsonl"))
    results = []
    try:
        await client.connect()
        for index, (text, expected) in enumerate(CASES, 1):
            args.tool_test_expect_tool = True
            error = ""
            try:
                await client.run_tool_test(text, output_dir / f"case_{index:02d}.wav", 60)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
            result = dict(client.last_tool_test_result or {})
            actual = signatures(result)
            results.append({
                "index": index,
                "text": text,
                "expected": expected,
                "actual": actual,
                "ok": bool(result.get("ok") and actual == expected and not error),
                "error": error,
            })
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
