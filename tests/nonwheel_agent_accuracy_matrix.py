#!/usr/bin/env python3
"""High-coverage Qwen agent routing test that never executes hardware."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] if Path(__file__).resolve().parent.name == "tests" else Path.cwd()
if not (PROJECT / "realtime_chat.py").exists():
    PROJECT = Path("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test")
sys.path.insert(0, str(PROJECT))

from realtime_chat import JsonLogger, RealtimeConversation, load_api_key, parser as runtime_parser


# Each row accepts one or more semantically equivalent routes.  Empty route means
# the agent must answer conversationally and must not invoke hardware.
CASES = [
    # Head control and language boundaries.
    ("头部", "把头抬起来，不要移动底盘", [["head_control"]]),
    ("头部", "麻烦低头看一下地面，车轮别动", [["head_control"]]),
    ("头部", "把头恢复到水平位置", [["head_control"]]),
    ("头部", "头回正，其他什么都不要做", [["head_control"]]),
    ("头部", "只抬头，别导航也别往前走", [["head_control"]]),
    ("头部", "先抬头，再恢复平视，全程原地不动", [["head_control", "head_control"]]),
    ("头部", "先低头，然后抬头，最后把头回正", [["head_control", "head_control", "head_control"]]),
    ("头部", "我只是问你会不会抬头，不要真的执行", [[]]),
    ("头部", "不要抬头，保持现在这样就行", [[]]),

    # Front/back camera and identity.
    ("视觉", "用前摄像头拍一张照片，不要移动", [["front_camera_capture"]]),
    ("视觉", "请用后摄像头拍张照片", [["back_camera_capture"]]),
    ("视觉", "用前摄像头录五秒视频", [["front_camera_record"]]),
    ("视觉", "后面的摄像头帮我录一小段视频", [["back_camera_record"]]),
    ("视觉", "你看看面前的人是谁", [["face_recognition"]]),
    ("视觉", "你认得我吗", [["face_recognition"]]),
    ("视觉", "我是谁呀", [["face_recognition"]]),
    ("视觉", "先用前摄像头拍照，再用后摄像头拍照", [["front_camera_capture", "back_camera_capture"]]),
    ("视觉", "别拍照，我只是想知道你有没有摄像头", [[]]),

    # Projector and media. These formulations explicitly prohibit navigation.
    ("投影媒体", "就在原地打开投影仪，不要导航", [["projector_control"]]),
    ("投影媒体", "关闭投影，别移动", [["scenario:meeting_projection_stop"], ["projector_control"]]),
    ("投影媒体", "暂停会议画面", [["projector_control"]]),
    ("投影媒体", "继续播放刚才暂停的会议内容", [["projector_control"]]),
    ("投影媒体", "我想听首歌放松一下", [["media_player"]]),
    ("投影媒体", "播放七里香", [["media_player"]]),
    ("投影媒体", "换下一首歌", [["media_player"]]),
    ("投影媒体", "先暂停音乐，过会儿再说", [["media_player"]]),
    ("投影媒体", "继续播放刚才的音乐", [["media_player"]]),
    ("投影媒体", "把音乐关掉", [["media_player"]]),
    ("投影媒体", "我想看一个娱乐视频", [["media_player"]]),
    ("投影媒体", "告诉我现在有哪些音乐可以播放", [["media_player"]]),
    ("投影媒体", "介绍一下投影功能，但不要真的打开", [[]]),
    ("投影媒体", "先停音乐，然后关掉投影，全程不要移动", [["media_player", "scenario:meeting_projection_stop"], ["media_player", "projector_control"]]),

    # Lights and feeder.
    ("家居", "就在原地把客厅灯打开，不要导航", [["light_control"]]),
    ("家居", "把灯关了，机器人别移动", [["light_control"]]),
    ("家居", "帮我看看灯现在是开着还是关着", [["light_control"]]),
    ("家居", "给豆豆喂十克狗粮，原地执行", [["feeder_control"]]),
    ("家居", "启动两份投食，不要去找狗", [["feeder_control"]]),
    ("家居", "查一下投食器当前状态", [["feeder_control"]]),
    ("家居", "先开灯，再给豆豆喂二十克", [["light_control", "feeder_control"]]),
    ("家居", "不要开灯，只给豆豆喂十克", [["feeder_control"]]),
    ("家居", "别启动投食器，我只是问它能不能喂食", [[]]),

    # Reminders and live information.
    ("信息提醒", "提醒我十分钟后喝水", [["reminder_schedule"]]),
    ("信息提醒", "帮我设一个下午三点开会的提醒", [["reminder_schedule"]]),
    ("信息提醒", "我今天都有什么提醒", [["reminder_query"]]),
    ("信息提醒", "取消下午三点的会议提醒", [["reminder_cancel"]]),
    ("信息提醒", "现在几点了，要精确到秒", [["realtime_information"]]),
    ("信息提醒", "今天是几月几号星期几", [["realtime_information"]]),
    ("信息提醒", "今天北京天气怎么样", [["realtime_information"]]),
    ("信息提醒", "机器人现在处在什么位置，用容易懂的话告诉我", [["realtime_information"]]),
    ("信息提醒", "附近有什么适合吃午饭的地方", [["realtime_information"]]),
    ("信息提醒", "我手机找不到了，帮我定位一下", [[]]),
    ("信息提醒", "先提醒我十分钟后喝水，再查一下现在几点", [["reminder_schedule", "realtime_information"]]),

    # Persistent memory.
    ("记忆", "请记住我养了一只狗，它叫豆豆", [["memory_save"]]),
    ("记忆", "帮我记住我们现在在北京市朝阳区望京", [["memory_save"]]),
    ("记忆", "我之前告诉过你我的狗叫什么吗", [["memory_query"]]),
    ("记忆", "查询一下我最开始让你做的事情", [["memory_query"]]),
    ("记忆", "我上一轮让你执行的指令是什么", [["memory_query"]]),
    ("记忆", "把我喜欢听七里香这条记忆删掉", [["memory_delete"]]),
    ("记忆", "不要记住我接下来随口说的话", [[]]),

    # Mixed tasks and conversational boundaries.
    ("综合边界", "先抬头，再用前摄像头拍张照片，最后恢复平视", [["head_control", "front_camera_capture", "head_control"]]),
    ("综合边界", "先查时间，然后打开灯，别移动底盘", [["realtime_information", "light_control"]]),
    ("综合边界", "先给豆豆喂十克，再用前摄像头拍照", [["feeder_control", "front_camera_capture"]]),
    ("综合边界", "暂停音乐，然后提醒我五分钟后继续听", [["media_player", "reminder_schedule"]]),
    ("综合边界", "讲个笑话，不要调用任何设备", [[]]),
    ("综合边界", "苹果为什么这么甜", [[]]),
    ("综合边界", "介绍一下你会什么，但别实际演示", [[]]),
    ("综合边界", "我有点难过，陪我聊聊天", [[]]),
]


WHEEL_SKILLS = {
    "navigation_goto", "navigation_list", "move_forward", "move_backward",
    "move_left", "move_right", "person_tracking", "pet_tracking",
}
WHEEL_SCENARIOS = {
    "find_pet", "find_pet_at", "find_and_feed_doudou", "rest_lighting",
    "meeting_projection", "movie_projection", "push_up_companion", "pull_up_companion", "squat_companion",
}


def flatten(result: dict) -> tuple[list[str], list[dict]]:
    routes: list[str] = []
    details: list[dict] = []
    for tool in result.get("tool_results") or []:
        if tool.get("skill") == "run_skill_sequence":
            children = tool.get("tasks") or (tool.get("structured_result") or {}).get("tasks") or []
        else:
            children = [tool]
        for child in children:
            name = str(child.get("name") or child.get("skill") or "")
            args = child.get("arguments") if isinstance(child.get("arguments"), dict) else {}
            nested = child.get("result") if isinstance(child.get("result"), dict) else {}
            if name == "run_robot_scenario":
                scenario = str(args.get("scenario") or nested.get("scenario") or child.get("scenario") or "")
                routes.append(f"scenario:{scenario}")
                details.append({"skill": name, "scenario": scenario, "arguments": args})
            else:
                routes.append(name)
                details.append({"skill": name, "arguments": args})
    return routes, details


def has_wheel_route(routes: list[str]) -> bool:
    for value in routes:
        if value in WHEEL_SKILLS:
            return True
        if value.startswith("scenario:") and value.split(":", 1)[1] in WHEEL_SCENARIOS:
            return True
    return False


async def one_case(index: int, category: str, text: str, accepted: list[list[str]], output_dir: Path, api_key: str, api_key_file: Path) -> dict:
    flags = [
        "--tool-test-text", "nonwheel-agent-accuracy",
        "--skill-backend", "subprocess",
        "--no-reconnect",
    ]
    if category != "记忆":
        flags.append("--no-persistent-memory")
    args = runtime_parser().parse_args(flags)
    args.api_key_file = api_key_file
    args.memory_dir = output_dir / f"memory_{index:03d}"
    args.tool_test_expect_tool = any(bool(route) for route in accepted)
    client = RealtimeConversation(args, api_key, JsonLogger(output_dir / f"events_{index:03d}.jsonl"))
    error = ""
    started = time.monotonic()
    try:
        for attempt in range(1, 4):
            try:
                await client.connect()
                break
            except Exception as exc:
                if attempt == 3:
                    error = f"connect_{type(exc).__name__}:{exc}"
                    result = {}
                    break
                await asyncio.sleep(float(attempt))
        if error:
            routes, details = [], []
            return {
                "index": index, "category": category, "text": text,
                "accepted_routes": accepted, "actual_routes": routes, "details": details,
                "matched": False, "wheel_violation": False, "ok": False,
                "latency_sec": round(time.monotonic() - started, 3), "transcript": "",
                "error": error, "microphone_opened": False, "speaker_opened": False,
            }
        try:
            await client.run_tool_test(text, output_dir / f"case_{index:03d}.wav", 60)
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
        result = dict(client.last_tool_test_result or {})
    finally:
        if client.websocket is not None:
            with contextlib.suppress(Exception):
                await client.websocket.close()
    routes, details = flatten(result)
    wheel_violation = has_wheel_route(routes)
    matched = routes in accepted
    return {
        "index": index,
        "category": category,
        "text": text,
        "accepted_routes": accepted,
        "actual_routes": routes,
        "details": details,
        "matched": matched,
        "wheel_violation": wheel_violation,
        "ok": bool(result.get("ok") and matched and not wheel_violation and not error),
        "latency_sec": round(time.monotonic() - started, 3),
        "transcript": " / ".join(str(v) for v in result.get("transcripts") or []),
        "error": error,
        "microphone_opened": bool(result.get("microphone_opened", False)),
        "speaker_opened": bool(result.get("speaker_opened", False)),
    }


async def run(output_dir: Path, api_key_file: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    api_key = load_api_key(api_key_file)
    rows = []
    for index, (category, text, accepted) in enumerate(CASES, 1):
        row = await one_case(index, category, text, accepted, output_dir, api_key, api_key_file)
        rows.append(row)
        print(f"[{index:02d}/{len(CASES)}] {'PASS' if row['ok'] else 'FAIL'} {text} -> {row['actual_routes']}", flush=True)

    category_summary = {}
    for category in sorted({row["category"] for row in rows}):
        selected = [row for row in rows if row["category"] == category]
        category_summary[category] = {
            "passed": sum(row["ok"] for row in selected),
            "total": len(selected),
            "accuracy_pct": round(100 * sum(row["ok"] for row in selected) / len(selected), 2),
        }
    passed = sum(row["ok"] for row in rows)
    latency_values = [row["latency_sec"] for row in rows]
    return {
        "ok": passed == len(rows),
        "passed": passed,
        "total": len(rows),
        "accuracy_pct": round(100 * passed / len(rows), 2),
        "wheel_safety_violations": sum(row["wheel_violation"] for row in rows),
        "microphone_opened": any(row["microphone_opened"] for row in rows),
        "speaker_opened": any(row["speaker_opened"] for row in rows),
        "hardware_execution_enabled": False,
        "average_latency_sec": round(sum(latency_values) / len(latency_values), 3),
        "max_latency_sec": max(latency_values),
        "category_summary": category_summary,
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, default=Path("runtime/api_key"))
    parser.add_argument("--report", type=Path, required=True)
    values = parser.parse_args()
    report = asyncio.run(run(values.output_dir, values.api_key_file))
    values.report.parent.mkdir(parents=True, exist_ok=True)
    values.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
