#!/usr/bin/env python3
"""Print one correlated timing trace from the runtime event log."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_EVENTS = Path("/home/test/qwen_robot_project/runtime/events/events.jsonl")


def load_events(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def latest_trace_id(records: list[dict[str, Any]]) -> str:
    for record in reversed(records):
        value = payload(record).get("trace_id")
        if value:
            return str(value)
    return ""


def build_report(records: list[dict[str, Any]], trace_id: str) -> dict[str, Any]:
    matched = [record for record in records if str(payload(record).get("trace_id") or "") == trace_id]
    event_by_type = {str(record.get("event_type") or ""): record for record in matched}
    pipeline = payload(event_by_type.get("voice_decision_pipeline_timing", {}))
    wakeup = payload(event_by_type.get("wakeup_session_stage_timing", {}))
    realtime = payload(event_by_type.get("realtime_stage_timing", {}))
    listen = payload(event_by_type.get("realtime_command_listen_finished", {}))
    task_group_ids = [str(item) for item in pipeline.get("task_group_ids") or []]

    step_timings: list[dict[str, Any]] = []
    for record in records:
        item = payload(record)
        if record.get("event_type") == "task_group_progress_saved" and str(item.get("task_group_id") or "") in task_group_ids:
            timing = item.get("step_timing")
            if isinstance(timing, dict) and timing:
                step_timings.append(
                    {
                        "task_group_id": item.get("task_group_id"),
                        "step_id": item.get("step_id"),
                        "skill_name": item.get("skill_name"),
                        "timing": timing,
                    }
                )
    step_ids = {str(step.get("step_id") or "") for step in step_timings}
    speech_timings: list[dict[str, Any]] = []
    for record in records:
        item = payload(record)
        if record.get("event_type") == "skill_speech_event":
            step_id = str(item.get("step_id") or "")
            if step_id in step_ids:
                speech_timings.append(
                    {
                        "step_id": step_id,
                        "skill_name": item.get("skill_name"),
                        "text": item.get("text"),
                        "elapsed_seconds": item.get("elapsed_seconds"),
                    }
                )

    return {
        "ok": bool(trace_id),
        "trace_id": trace_id,
        "wakeup_event_id": realtime.get("wakeup_event_id") or wakeup.get("wakeup_event_id"),
        "session_id": pipeline.get("session_id") or wakeup.get("session_id"),
        "task_group_ids": task_group_ids,
        "semantic_adjudication_completed": bool(listen.get("semantic_adjudication_completed")),
        "voice_decision": {
            "durations_ms": realtime.get("durations_ms") or {},
            "marks_ms_from_turn_start": realtime.get("marks_ms_from_turn_start") or {},
            "counters": realtime.get("counters") or {},
        },
        "decision_pipeline": pipeline.get("durations_ms") or {},
        "wakeup_session": wakeup.get("durations_ms") or {},
        "skill_steps": step_timings,
        "skill_speech": speech_timings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Show detailed latency for the latest or selected voice trace.")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--trace-id", default="")
    args = parser.parse_args()
    records = load_events(args.events)
    trace_id = str(args.trace_id or latest_trace_id(records))
    report = build_report(records, trace_id)
    if not trace_id:
        report["error"] = "no_timing_trace_found"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if trace_id else 1


if __name__ == "__main__":
    raise SystemExit(main())
