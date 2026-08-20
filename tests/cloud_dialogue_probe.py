#!/usr/bin/env python3
"""Two-turn Qwen probe that never opens audio devices or real hardware."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realtime_chat import JsonLogger, RealtimeConversation, load_api_key, parser as runtime_parser


async def run(first: str, second: str, output_dir: Path, api_key_file: Path) -> dict:
    args = runtime_parser().parse_args(
        [
            "--tool-test-text", "dialogue-probe",
            "--skill-backend", "subprocess",
            "--no-reconnect",
        ]
    )
    args.api_key_file = api_key_file
    output_dir.mkdir(parents=True, exist_ok=True)
    client = RealtimeConversation(
        args,
        load_api_key(api_key_file),
        JsonLogger(output_dir / "events.jsonl"),
    )
    try:
        await client.connect()
        args.tool_test_expect_tool = False
        await client.run_tool_test(first, output_dir / "turn_1.wav", 55)
        first_result = dict(client.last_tool_test_result or {})
        args.tool_test_expect_tool = True
        await client.run_tool_test(second, output_dir / "turn_2.wav", 55)
        second_result = dict(client.last_tool_test_result or {})
        return {
            "ok": bool(first_result.get("ok") and second_result.get("ok")),
            "first": first_result,
            "second": second_result,
            "microphone_opened": False,
            "speaker_opened": False,
        }
    finally:
        if client.websocket is not None:
            await client.websocket.close()


def main() -> int:
    cli = argparse.ArgumentParser()
    cli.add_argument("--first", required=True)
    cli.add_argument("--second", required=True)
    cli.add_argument("--output-dir", type=Path, required=True)
    cli.add_argument("--api-key-file", type=Path, default=Path("runtime/api_key"))
    values = cli.parse_args()
    result = asyncio.run(run(values.first, values.second, values.output_dir, values.api_key_file))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
