#!/usr/bin/env python3
"""Focused Qwen routing test; no microphone, speaker, ROS, or hardware."""

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
    ("你知道我是谁吗？", "face_recognition", None),
    ("我是谁？", "face_recognition", None),
    ("你认得我吗？", "face_recognition", None),
    ("你现在电量还有多少？", None, None),
    ("现在几点了？", "realtime_information", "current_time"),
    ("今天是几月几号，星期几？", "realtime_information", "current_time"),
    ("告诉我现在的年月日时分秒。", "realtime_information", "current_time"),
    ("我手机找不到了，帮我定位一下。", None, None),
    ("我的手机现在在哪里？", None, None),
    ("我想听歌放松一下。", "media_player", "play_music"),
    ("播放七里香。", "media_player", "play_music"),
    ("换一首歌。", "media_player", "next"),
    ("暂停播放。", "media_player", "pause"),
    ("继续播放。", "media_player", "resume"),
    ("结束播放。", "media_player", "stop"),
    ("我想看一个好看的娱乐视频。", "media_player", "play_video"),
    ("理想同学，你能做什么？", None, None),
    ("你都有哪些功能？", None, None),
    ("再换个说法介绍一下自己。", None, None),
]


def result_action(tool_result: dict) -> str | None:
    arguments = tool_result.get("arguments")
    if isinstance(arguments, dict) and arguments.get("action"):
        return str(arguments["action"])
    structured = tool_result.get("structured_result")
    if isinstance(structured, dict) and structured.get("action"):
        return str(structured["action"])
    return None


async def run(output_dir: Path, api_key_file: Path, *, variation_only: bool = False) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    api_key = load_api_key(api_key_file)
    rows = []
    cases = [] if variation_only else CASES[:-3]
    for index, (text, expected_tool, expected_action) in enumerate(cases, 1):
        args = runtime_parser().parse_args([
            "--tool-test-text", "focused-matrix",
            "--skill-backend", "subprocess",
            "--no-reconnect",
            "--no-persistent-memory",
        ])
        args.api_key_file = api_key_file
        args.memory_dir = output_dir / f"memory_{index:02d}"
        client = RealtimeConversation(args, api_key, JsonLogger(output_dir / f"events_{index:02d}.jsonl"))
        try:
            await client.connect()
            args.tool_test_expect_tool = expected_tool is not None
            error = None
            try:
                await client.run_tool_test(text, output_dir / f"case_{index:02d}.wav", 60)
            except Exception as exc:
                error = f"{type(exc).__name__}:{exc}"
            value = dict(client.last_tool_test_result or {})
            tools = list(value.get("tool_results") or [])
            actual = [str(item.get("skill") or "") for item in tools]
            actions = [result_action(item) for item in tools]
            matched = (
                not actual if expected_tool is None
                else expected_tool in actual
                and (expected_action is None or expected_action in actions)
            )
            rows.append({
                "text": text,
                "expected_tool": expected_tool,
                "expected_action": expected_action,
                "actual_tools": actual,
                "actual_actions": actions,
                "transcript": " / ".join(str(item) for item in value.get("transcripts") or []),
                "ok": bool(value.get("ok") and matched and error is None),
                "error": error,
            })
        finally:
            if client.websocket is not None:
                with contextlib.suppress(Exception):
                    await client.websocket.close()

    async def no_tool_dialogue(name: str, texts: list[str]) -> list[str]:
        args = runtime_parser().parse_args([
            "--tool-test-text", name,
            "--skill-backend", "subprocess",
            "--no-reconnect",
            "--no-persistent-memory",
        ])
        args.api_key_file = api_key_file
        args.memory_dir = output_dir / f"memory_{name}"
        client = RealtimeConversation(args, api_key, JsonLogger(output_dir / f"events_{name}.jsonl"))
        replies = []
        try:
            await client.connect()
            for index, text in enumerate(texts, 1):
                args.tool_test_expect_tool = False
                await client.run_tool_test(text, output_dir / f"{name}_{index:02d}.wav", 60)
                value = dict(client.last_tool_test_result or {})
                if value.get("tool_results"):
                    raise AssertionError(f"unexpected_tool:{name}:{index}")
                replies.append(" / ".join(str(item) for item in value.get("transcripts") or []))
        finally:
            if client.websocket is not None:
                with contextlib.suppress(Exception):
                    await client.websocket.close()
        return replies

    intro_replies = await no_tool_dialogue("intro", [item[0] for item in CASES[-3:]])
    phone_replies = await no_tool_dialogue(
        "phone_boundary",
        ["我手机找不到了，帮我定位一下。", "我的手机现在在哪里？"],
    )
    distinct_intros = len(set(intro_replies)) == len(intro_replies)
    distinct_phone_boundaries = len(set(phone_replies)) == len(phone_replies)
    intro_boundaries_ok = not any(
        marker in reply
        for reply in intro_replies
        for marker in ("我是家人", "像个家人", "像家人", "像真人", "我是真人")
    )
    return {
        "ok": all(row["ok"] for row in rows) and distinct_intros and distinct_phone_boundaries and intro_boundaries_ok,
        "passed": sum(row["ok"] for row in rows),
        "total": len(rows),
        "distinct_intro_replies": distinct_intros,
        "intro_replies": intro_replies,
        "intro_capability_boundaries_ok": intro_boundaries_ok,
        "distinct_phone_boundary_replies": distinct_phone_boundaries,
        "phone_boundary_replies": phone_replies,
        "results": rows,
        "microphone_opened": False,
        "speaker_opened": False,
        "hardware_opened": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, default=Path("runtime/api_key"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--variation-only", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(args.output_dir, args.api_key_file, variation_only=args.variation_only))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
