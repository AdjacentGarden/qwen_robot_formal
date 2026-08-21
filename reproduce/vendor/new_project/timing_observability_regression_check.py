#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from new_project.doubao_realtime import DoubaoRealtimeSession
from timing_report import build_report


def main() -> int:
    session = DoubaoRealtimeSession.__new__(DoubaoRealtimeSession)
    args = argparse.Namespace(
        _turn_timing={
            "trace_id": "voice_test",
            "turn_started_at": 10.0,
            "record_started_at": 10.1,
            "voice_detected_at": 10.3,
            "vad_finished_at": 10.9,
            "tail_started_at": 10.9,
            "tail_sent_at": 11.0,
            "primary.receive_started_at": 10.1,
            "primary.audio_sender_done_at": 11.0,
            "primary.first_model_text_at": 11.4,
            "primary.first_json_signal_at": 11.45,
            "primary.decision_ready_at": 11.8,
            "primary.receive_finished_at": 11.8,
            "turn_finished_at": 12.0,
            "counters": {"primary.prompt_chars": 20590},
        }
    )
    timing = session._timing_report(args)
    assert timing["trace_id"] == "voice_test"
    assert timing["durations_ms"]["turn_total"] == 2000.0
    assert timing["durations_ms"]["primary_wait_for_voice"] == 200.0
    assert timing["durations_ms"]["primary_voice_capture"] == 600.0
    assert timing["durations_ms"]["primary_first_model_text_to_json_ready"] == 400.0

    events = [
        {
            "event_type": "realtime_stage_timing",
            "payload": {"trace_id": "voice_test", **timing},
        },
        {
            "event_type": "realtime_command_listen_finished",
            "payload": {"trace_id": "voice_test", "semantic_adjudication_completed": True},
        },
        {
            "event_type": "voice_decision_pipeline_timing",
            "payload": {
                "trace_id": "voice_test",
                "session_id": "session_test",
                "task_group_ids": ["task_test"],
                "durations_ms": {"pipeline_total": 1200.0},
            },
        },
        {
            "event_type": "wakeup_session_stage_timing",
            "payload": {"trace_id": "voice_test", "durations_ms": {"session_total": 5000.0}},
        },
        {
            "event_type": "task_group_progress_saved",
            "payload": {
                "task_group_id": "task_test",
                "step_id": "step_test",
                "skill_name": "light_control",
                "step_timing": {"total_ms": 1010.0, "speech_callback_total_ms": 2000.0},
            },
        },
        {
            "event_type": "skill_speech_event",
            "payload": {
                "step_id": "step_test",
                "skill_name": "light_control",
                "text": "落地灯已打开",
                "elapsed_seconds": 2.0,
            },
        },
    ]
    report = build_report(events, "voice_test")
    assert report["semantic_adjudication_completed"] is True
    assert report["session_id"] == "session_test"
    assert report["skill_steps"][0]["timing"]["total_ms"] == 1010.0
    assert report["skill_speech"][0]["elapsed_seconds"] == 2.0
    print(json.dumps({"ok": True, "timing": timing, "report": report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
