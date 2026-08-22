import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from projection_occlusion_observer import (
    DEFAULT_ALERT,
    Detection,
    OcclusionStateMachine,
    ProjectionOcclusionObserver,
    annotate_projection_roi,
    encode_jpeg,
    parse_detection,
    projector_session_active,
)
from realtime_chat import JsonLogger, RealtimeConversation


def test_parse_detection_and_fail_closed_fields():
    result = parse_detection(
        '```json\n{"blocked":true,"confidence":0.93,"person_position":"center",'
        '"suggested_move":"right","reason":"人物挡住中央"}\n```',
        88.0,
    )
    assert result == Detection(True, 0.93, "center", "right", "人物挡住中央", 88.0)


def test_state_machine_confirms_repeats_and_clears():
    state = OcclusionStateMachine(
        blocked_required=2,
        clear_required=2,
        confidence_threshold=0.7,
        repeat_seconds=10,
    )
    blocked = Detection(True, 0.85, "center", "right", "blocked")
    clear = Detection(False, 0.95, "none", "none", "clear")
    assert state.update(blocked, now=0) == []
    first = state.update(blocked, now=1)
    assert [item.kind for item in first] == ["blocked"]
    assert first[0].text == DEFAULT_ALERT
    assert state.update(blocked, now=5) == []
    assert [item.kind for item in state.update(blocked, now=11)] == ["blocked_repeat"]
    assert state.update(clear, now=12) == []
    assert [item.kind for item in state.update(clear, now=13)] == ["clear"]


def test_high_confidence_blocking_alerts_on_first_frame():
    state = OcclusionStateMachine(
        blocked_required=2,
        clear_required=2,
        confidence_threshold=0.7,
        immediate_confidence_threshold=0.92,
        repeat_seconds=10,
    )
    blocked = Detection(True, 0.95, "center", "right", "blocked")
    alerts = state.update(blocked, now=0)
    assert [item.kind for item in alerts] == ["blocked"]
    assert alerts[0].text == DEFAULT_ALERT


def test_projector_state_is_authoritative(tmp_path):
    state = tmp_path / "projector.json"
    assert projector_session_active(state) is False
    state.write_text(json.dumps({"session_active": True}), encoding="utf-8")
    assert projector_session_active(state) is True
    state.write_text(json.dumps({"session_active": False}), encoding="utf-8")
    assert projector_session_active(state) is False


def test_annotation_is_jpeg_under_api_limit():
    frame = np.full((480, 640, 3), 180, dtype=np.uint8)
    annotated = annotate_projection_roi(
        frame,
        [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)],
    )
    jpeg = encode_jpeg(annotated)
    assert jpeg[:2] == b"\xff\xd8"
    assert len(jpeg) <= 190_000


def test_observer_only_opens_camera_while_meeting_active(tmp_path):
    async def exercise():
        state_path = tmp_path / "projector.json"
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "camera": "/dev/fake-front",
                    "projector_state_file": str(state_path),
                    "roi": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                    "interval_seconds": 1.0,
                    "state_poll_seconds": 0.01,
                    "blocked_confirmations": 2,
                    "clear_confirmations": 2,
                    "confidence_threshold": 0.7,
                    "repeat_seconds": 10,
                }
            ),
            encoding="utf-8",
        )
        opened = []
        detections = []

        class Capture:
            def isOpened(self):
                return True

            def read(self):
                return True, np.full((48, 64, 3), 100, dtype=np.uint8)

            def release(self):
                opened.append("released")

        class Session:
            def __init__(self, *_args, **_kwargs):
                pass

            async def detect(self, _jpeg):
                detections.append(True)
                return Detection(False, 0.9, "none", "none", "clear", 1.0)

            async def close(self):
                return None

        def capture_factory(source):
            opened.append(source)
            return Capture()

        observer = ProjectionOcclusionObserver(
            "test-key",
            config_path=config_path,
            event_callback=lambda _event: True,
            log=lambda *_args, **_kwargs: None,
            capture_factory=capture_factory,
            session_factory=Session,
        )
        await observer.start()
        await asyncio.sleep(0.04)
        assert opened == []
        state_path.write_text(json.dumps({"session_active": True}), encoding="utf-8")
        for _ in range(50):
            if detections:
                break
            await asyncio.sleep(0.01)
        assert detections
        assert opened[0] == "/dev/fake-front"
        state_path.write_text(json.dumps({"session_active": False}), encoding="utf-8")
        for _ in range(50):
            if "released" in opened:
                break
            await asyncio.sleep(0.01)
        assert "released" in opened
        await observer.stop()

    asyncio.run(exercise())


def test_conversation_routes_alert_through_existing_speaker(tmp_path):
    queued = []

    class Speaker:
        def submit_from_thread(self, event):
            queued.append(dict(event))

    conversation = RealtimeConversation(
        SimpleNamespace(projection_occlusion=False),
        "sk-test",
        JsonLogger(tmp_path / "events.jsonl"),
    )
    conversation.skill_event_speaker = Speaker()
    conversation.user_turn_id = 7
    delivered = conversation.handle_projection_occlusion_event(
        {
            "event_id": "occlusion-1",
            "skill_name": "projector_control",
            "kind": "attention",
            "text": DEFAULT_ALERT,
        }
    )
    assert delivered is True
    assert queued == [
        {
            "event_id": "occlusion-1",
            "skill_name": "projector_control",
            "kind": "attention",
            "text": DEFAULT_ALERT,
            "turn_id": "7",
        }
    ]


def test_conversation_defers_alert_when_speaker_is_reconnecting(tmp_path):
    conversation = RealtimeConversation(
        SimpleNamespace(projection_occlusion=False),
        "sk-test",
        JsonLogger(tmp_path / "events.jsonl"),
    )
    assert conversation.handle_projection_occlusion_event(
        {"kind": "attention", "text": DEFAULT_ALERT}
    ) is False
