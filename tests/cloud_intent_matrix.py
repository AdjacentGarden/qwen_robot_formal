#!/usr/bin/env python3
"""Cloud Function Calling matrix; never opens audio devices or real hardware."""

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
    ("现在几点了？", None, None),
    ("不看了，我已经坐了一天了", None, None),
    ("哈喽理想同学，我回来了", "run_robot_scenario", "homecoming_welcome"),
    ("陪我做俯卧撑吧", "run_robot_scenario", "push_up_companion"),
    ("陪我做引体向上", "run_robot_scenario", "pull_up_companion"),
    ("陪我做深蹲", "run_robot_scenario", "squat_companion"),
    ("去所有保存的地点找豆豆", "run_robot_scenario", "find_pet"),
    ("只去书房找豆豆", "run_robot_scenario", "find_pet_at"),
    ("就在这里找豆豆", "run_robot_scenario", "find_pet_here"),
    ("豆豆该吃饭了，找到后给它喂食", "run_robot_scenario", "find_and_feed_doudou"),
    ("陪我到书房开会并投影", "run_robot_scenario", "meeting_projection"),
    ("结束会议投影", "run_robot_scenario", "meeting_projection_stop"),
    ("我想休息一会，帮我调整灯光", "run_robot_scenario", "rest_lighting"),
    ("客厅太暗了，打开客厅灯", "run_robot_scenario", "living_room_light_service"),
    ("请用前摄像头拍一张照片", "front_camera_capture", None),
    ("请用后摄像头录一段视频", "back_camera_record", None),
    ("看看面前的人是谁", "face_recognition", None),
    ("把我的人脸注册为测试用户", "face_registration", None),
    ("提醒我十分钟后喝水", "reminder_schedule", None),
    ("查询我有哪些提醒", "reminder_query", None),
    ("删除刚才的喝水提醒", "reminder_cancel", None),
    ("机器人可以导航到哪些保存点？", "navigation_list", None),
    ("导航到原点", "navigation_goto", None),
    ("今天天气怎么样？", "realtime_information", None),
    ("机器人当前自身在哪里？", "realtime_information", None),
    ("投食机出粮二十克", "feeder_control", None),
    ("跟踪前面的人", "person_tracking", None),
    ("记住我喜欢喝温水", "memory_save", None),
    ("你记得我喜欢喝什么吗", "memory_query", None),
    ("忘掉我喜欢喝温水这件事", "memory_delete", None),
]


async def run(output_dir: Path, api_key_file: Path) -> dict:
    args = runtime_parser().parse_args(["--tool-test-text", "matrix", "--skill-backend", "subprocess", "--no-reconnect"])
    args.api_key_file = api_key_file
    args.memory_dir = output_dir / "memory"
    output_dir.mkdir(parents=True, exist_ok=True)
    client = RealtimeConversation(args, load_api_key(api_key_file), JsonLogger(output_dir / "events.jsonl"))
    results = []
    try:
        await client.connect()
        for index, (text, expected_tool, expected_scenario) in enumerate(CASES, 1):
            args.tool_test_expect_tool = expected_tool is not None
            error = None
            try:
                await client.run_tool_test(text, output_dir / f"case_{index:02d}.wav", 60)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
            result = dict(client.last_tool_test_result or {})
            tools = result.get("tool_results") or []
            actual_tools = [str(item.get("skill") or "") for item in tools]
            actual_scenarios = [str(item.get("scenario") or "") for item in tools if item.get("scenario")]
            matched = (
                (not actual_tools)
                if expected_tool is None
                else expected_tool in actual_tools and (expected_scenario is None or expected_scenario in actual_scenarios)
            )
            results.append({
                "index": index,
                "text": text,
                "expected_tool": expected_tool,
                "expected_scenario": expected_scenario,
                "actual_tools": actual_tools,
                "actual_scenarios": actual_scenarios,
                "truthful_transcript": result.get("truthful_transcript"),
                "ok": bool(result.get("ok") and matched and not error),
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
    cli.add_argument("--report", type=Path)
    values = cli.parse_args()
    report = asyncio.run(run(values.output_dir, values.api_key_file))
    if values.report is not None:
        values.report.parent.mkdir(parents=True, exist_ok=True)
        values.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
