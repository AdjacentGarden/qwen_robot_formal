#!/usr/bin/env python3
"""Cloud Memory routing/answer matrix; opens no microphone, speaker, ROS, or hardware."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_store import MemoryStore
from realtime_chat import JsonLogger, RealtimeConversation, load_api_key, parser as runtime_parser


CASES = (
    ("我上一条让你执行的指令是什么？", "commands", ["结束会议投影"], None),
    ("往前数两轮，我让你执行的指令是什么？", "commands", ["开始会议投影"], None),
    ("最近前两轮我让你执行了什么？", "commands", ["开始会议投影", "结束会议投影"], None),
    ("最开始让我执行的指令是什么？", "commands", ["打开客厅灯"], None),
    ("从最早开始数，第二条指令是什么？", "commands", ["导航到书房"], None),
    ("我今天几点让你寻找豆豆？", "commands", ["寻找豆豆"], "09:10"),
    ("今天我让你执行过哪些指令？", "commands", ["寻找豆豆", "播放轻松音乐", "开始会议投影", "结束会议投影"], None),
    ("请查询长期记忆：我的狗叫什么名字？", "facts", ["豆豆"], None),
    ("请查询长期记忆：机器人当前常驻在哪个城市和区？", "facts", ["北京", "朝阳区"], None),
)


def seed_commands(store: MemoryStore) -> None:
    zone = ZoneInfo("Asia/Shanghai")
    today = datetime.now(zone).replace(hour=8, minute=0, second=0, microsecond=0)
    rows = (
        (today - timedelta(days=2), "打开客厅灯", "light_control", {"action": "on"}),
        (today - timedelta(days=1), "导航到书房", "navigation_goto", {"point": "study_projection"}),
        (today.replace(hour=9, minute=10), "寻找豆豆", "pet_tracking", {"action": "find"}),
        (today.replace(hour=10, minute=20), "播放轻松音乐", "media_player", {"action": "play_music"}),
        (today.replace(hour=11, minute=30), "开始会议投影", "run_robot_scenario", {"scenario": "meeting_projection"}),
        (today.replace(hour=12, minute=40), "结束会议投影", "run_robot_scenario", {"scenario": "meeting_projection_stop"}),
    )
    for turn, (stamp, text, skill, arguments) in enumerate(rows, 1):
        store.record_command(
            user_text=text,
            session_id="cloud-memory-matrix",
            turn_id=turn,
            skill=skill,
            arguments=arguments,
            result={"ok": True, "executed": True, "mode": "execute", "spoken_summary": "完成"},
            received_at=stamp.timestamp(),
        )


async def run(output_dir: Path, api_key_file: Path, profile: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    memory_dir = output_dir / "memory"
    store = MemoryStore(memory_dir, profile_path=profile)
    seed_commands(store)

    args = runtime_parser().parse_args([
        "--tool-test-text", "memory-matrix",
        "--skill-backend", "subprocess",
        "--no-reconnect",
    ])
    args.api_key_file = api_key_file
    args.memory_dir = memory_dir
    args.memory_profile_config = profile
    args.tool_test_expect_tool = True
    api_key = load_api_key(api_key_file)
    client = RealtimeConversation(args, api_key, JsonLogger(output_dir / "events.jsonl"))
    rows = []
    try:
        await client.connect()
        for index, (text, result_key, expected_terms, expected_time) in enumerate(CASES, 1):
            error = None
            try:
                await client.run_tool_test(text, output_dir / f"case_{index:02d}.wav", 60)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
            value = dict(client.last_tool_test_result or {})
            memory_results = [
                item for item in value.get("tool_results") or []
                if item.get("skill") == "memory_query"
            ]
            memory_result = memory_results[-1] if memory_results else {}
            items = list(memory_result.get(result_key) or [])
            rendered = json.dumps(items, ensure_ascii=False)
            transcript = " / ".join(str(item) for item in value.get("transcripts") or [])
            transcript_compact = re.sub(r"\s+", "", transcript)
            terms_ok = all(term in rendered and term in transcript for term in expected_terms)
            spoken_time = ""
            if expected_time is not None:
                hour, minute = expected_time.split(":", 1)
                spoken_time = f"{int(hour)}点{int(minute)}分"
            time_ok = expected_time is None or (
                expected_time in rendered
                and (expected_time in transcript or spoken_time in transcript_compact)
            )
            no_hardware_tool = all(
                item.get("skill") == "memory_query"
                for item in value.get("tool_results") or []
            )
            ok = bool(
                value.get("ok")
                and memory_result.get("ok")
                and terms_ok
                and time_ok
                and no_hardware_tool
                and error is None
            )
            rows.append(
                {
                    "text": text,
                    "ok": ok,
                    "query_type": memory_result.get("query_type"),
                    "returned": items,
                    "transcript": transcript,
                    "tool_names": [item.get("skill") for item in value.get("tool_results") or []],
                    "error": error,
                }
            )
    finally:
        if client.websocket is not None:
            with contextlib.suppress(Exception):
                await client.websocket.close()
    return {
        "ok": all(item["ok"] for item in rows),
        "passed": sum(item["ok"] for item in rows),
        "total": len(rows),
        "results": rows,
        "microphone_opened": False,
        "speaker_opened": False,
        "ros_opened": False,
        "hardware_opened": False,
    }


def main() -> int:
    value = argparse.ArgumentParser()
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--api-key-file", type=Path, default=Path("runtime/api_key"))
    value.add_argument("--profile", type=Path, default=Path("config/resident_profile.json"))
    value.add_argument("--report", type=Path, required=True)
    args = value.parse_args()
    report = asyncio.run(run(args.output_dir, args.api_key_file, args.profile))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
