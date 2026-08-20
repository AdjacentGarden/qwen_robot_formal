#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import time
import tempfile
from pathlib import Path

from realtime_chat import load_api_key
from skill_event_audio import QwenSkillEventSpeaker


async def main() -> None:
    parser = argparse.ArgumentParser(description="No-device Qwen skill acknowledgement latency probe")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--text", default="收到，我现在开始处理。")
    parser.add_argument("--voice", default="longanqian")
    args = parser.parse_args()
    logs: list[tuple[str, dict]] = []
    chunks: list[bytes] = []
    with tempfile.TemporaryDirectory() as temporary:
        speaker = QwenSkillEventSpeaker(
            api_key=load_api_key(args.api_key_file),
            voice=args.voice,
            workspace="",
            region="cn-beijing",
            endpoint="",
            connect_timeout=15.0,
            enqueue_pcm=chunks.append,
            log=lambda event, **fields: logs.append((event, fields)),
            cache_dir=Path(temporary),
        )
        await speaker._connect()
        started = time.monotonic()
        event = {"skill_name": "latency_probe", "kind": "acknowledgement", "text": args.text}
        await speaker._synthesize(event)
        total_ms = round((time.monotonic() - started) * 1000.0, 1)
        cached_started = time.monotonic()
        await speaker._synthesize(event)
        cached_ms = round((time.monotonic() - cached_started) * 1000.0, 1)
        await speaker.websocket.close()
    first = next(fields for event, fields in logs if event == "skill_event_audio_first_chunk")
    generated = next(fields for event, fields in logs if event == "skill_event_audio_generated")
    print(
        json.dumps(
            {
                "ok": bool(chunks),
                "hardware_opened": False,
                "first_playable_audio_ms": first["latency_ms"],
                "complete_synthesis_ms": generated["latency_ms"],
                "measured_total_ms": total_ms,
                "pcm_bytes": sum(map(len, chunks)),
                "stream_chunk_count": len(chunks),
                "cached_replay_ms": cached_ms,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
