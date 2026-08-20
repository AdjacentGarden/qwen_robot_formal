#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from memory_store import MemoryStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill durable command memory from realtime JSONL logs")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--memory-dir", type=Path, required=True)
    args = parser.parse_args()
    store = MemoryStore(args.memory_dir)
    session_index = 0
    turn_index = 0
    active: dict | None = None
    live_audio_seen = False
    calls = 0
    for line in args.log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = str(row.get("event") or "")
        if event == "session_ready":
            session_index += 1
            turn_index = 0
            active = None
            live_audio_seen = False
        elif event == "program_stopped":
            active = None
            live_audio_seen = False
        elif event == "service_event" and row.get("type") == "input_audio_buffer.speech_started":
            live_audio_seen = True
        elif event == "input_transcript":
            turn_index += 1
            active = (
                {
                    "text": str(row.get("text") or "").strip(),
                    "ts": float(row.get("ts") or 0),
                    "turn": turn_index,
                }
                if live_audio_seen
                else None
            )
        elif event == "local_skill_result" and active and active["text"]:
            # A result arriving hours later belongs to a later synthetic test
            # or stale connection, not to the last remembered live utterance.
            if float(row.get("ts") or 0) - active["ts"] > 180:
                active = None
                continue
            if str(row.get("mode") or "") in {"dry_run", "intent_rejected", "deduplicated"}:
                continue
            store.record_command(
                user_text=active["text"],
                session_id=f"legacy_log_session_{session_index}",
                turn_id=active["turn"],
                skill=str(row.get("skill") or "unknown_skill"),
                arguments={},
                result={
                    "ok": bool(row.get("ok")),
                    "validation_ok": bool(row.get("validation_ok")),
                    "executed": bool(row.get("executed")),
                    "mode": row.get("mode"),
                    "error": row.get("error"),
                    "spoken_summary": "",
                },
                received_at=active["ts"],
            )
            calls += 1
    records = store.invoke(
        "memory_query",
        {"scope": "command_history", "query_type": "search", "limit": 50},
    )["commands"]
    print(
        json.dumps(
            {
                "ok": True,
                "source_calls_seen": calls,
                "latest_records_returned": len(records),
                "memory_dir": str(args.memory_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
