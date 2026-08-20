import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def make_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROBOT_APP_TOKEN", "app-test-token")
    monkeypatch.setenv("ROBOT_BRIDGE_TOKEN", "robot-test-token")
    module = importlib.import_module("server.app")
    module.DATA = tmp_path
    module.VIDEOS = tmp_path / "videos"
    module.THUMBS = tmp_path / "thumbs"
    module.MAP_FILE = tmp_path / "map.png"
    module.STATE_FILE = tmp_path / "state.json"
    module.VIDEO_INDEX = tmp_path / "videos.json"
    module.VIDEOS.mkdir()
    module.THUMBS.mkdir()
    module.APP_TOKEN = "app-test-token"
    module.ROBOT_TOKEN = "robot-test-token"
    module.hub = module.Hub()
    return TestClient(module.app), module


def test_health_and_auth(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    with client:
        assert client.get("/api/health").json()["ok"] is True
        assert client.get("/api/state", params={"token": "bad"}).status_code == 401
        assert client.get("/api/state", params={"token": "app-test-token"}).status_code == 200


def test_command_validation(tmp_path, monkeypatch):
    _, module = make_client(tmp_path, monkeypatch)
    move = module.validate_command({"action": "manual_move", "direction": "forward", "duration": 99})
    assert move["duration"] == 0.45
    assert move["linear_speed"] == 0.12
    nav = module.validate_command({"action": "navigate", "x": 1, "y": -2, "yaw": 3.14})
    assert nav["x"] == 1.0 and nav["y"] == -2.0
    assert module.validate_command({"action": "program_start"})["action"] == "program_start"
    assert module.validate_command({"action": "program_stop"})["action"] == "program_stop"
    assert module.validate_command({"action": "program_status"})["action"] == "program_status"
    assert module.validate_command({"action": "microphone_set", "enabled": False})["enabled"] is False
    assert module.validate_command({"action": "microphone_set", "enabled": True})["enabled"] is True
    assert module.validate_command({"action": "light", "state": "on"})["state"] == "on"
    assert module.validate_command({"action": "feed", "grams": 20})["grams"] == 20
    assert module.validate_command({"action": "stop"})["action"] == "stop"
    assert module.validate_command({"action": "request_state"})["action"] == "request_state"


def test_command_validation_rejects_every_invalid_button_branch(tmp_path, monkeypatch):
    _, module = make_client(tmp_path, monkeypatch)
    invalid = [
        ({"action": "manual_move", "direction": "diagonal"}, "invalid_direction"),
        ({"action": "light", "state": "dim"}, "invalid_light_state"),
        ({"action": "feed", "grams": 15}, "invalid_feed_grams"),
        ({"action": "navigate", "x": 101, "y": 0}, "navigation_target_out_of_range"),
        ({"action": "microphone_set", "enabled": "false"}, "microphone_enabled_must_be_boolean"),
        ({"action": "unknown"}, "unsupported_action"),
    ]
    for command, expected in invalid:
        with pytest.raises(ValueError, match=expected):
            module.validate_command(command)


def test_robot_and_app_websocket_relay(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    with client:
        with client.websocket_connect("/ws/robot?token=robot-test-token") as robot:
            robot.send_json({"type": "hello", "robot": {"name": "test-robot"}})
            with client.websocket_connect("/ws/app?token=app-test-token") as phone:
                state = phone.receive_json()
                assert state["type"] == "state"
                phone.send_json({"id": "c1", "action": "light", "state": "on"})
                assert robot.receive_json()["action"] == "light"
                assert phone.receive_json()["ok"] is True


def test_program_state_relay(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    with client:
        with client.websocket_connect("/ws/robot?token=robot-test-token") as robot:
            robot.send_json({"type": "telemetry", "pose": None, "program": {"state": "running"}})
            with client.websocket_connect("/ws/app?token=app-test-token") as phone:
                state = phone.receive_json()
                assert state["program"]["state"] == "running"
                phone.send_json({"id": "p1", "action": "program_stop"})
                assert robot.receive_json()["action"] == "program_stop"
                assert phone.receive_json()["ok"] is True


def test_microphone_state_relay_preserves_app_voice_capability(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    microphone = {
        "enabled": False,
        "accepting_local_voice": False,
        "app_voice_enabled": True,
    }
    with client:
        with client.websocket_connect("/ws/robot?token=robot-test-token") as robot:
            robot.send_json({"type": "telemetry", "pose": None, "microphone": microphone})
            with client.websocket_connect("/ws/app?token=app-test-token") as phone:
                state = phone.receive_json()
                assert state["microphone"] == microphone
                phone.send_json({"id": "mic-on", "action": "microphone_set", "enabled": True})
                relayed = robot.receive_json()
                assert relayed["action"] == "microphone_set"
                assert relayed["enabled"] is True
                assert phone.receive_json()["ok"] is True


def test_streamed_app_voice_relay_and_empty_audio_branch(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    with client:
        with client.websocket_connect("/ws/robot?token=robot-test-token") as robot:
            robot.send_json({"type": "hello", "robot": {"name": "test-robot"}})
            with client.websocket_connect("/ws/app?token=app-test-token") as phone:
                assert phone.receive_json()["type"] == "state"

                phone.send_json({"id": "voice-ok", "action": "voice_stream_start", "mime_type": "audio/webm;codecs=opus"})
                assert phone.receive_json()["status"] == "voice_stream_ready"
                phone.send_json({"id": "voice-ok", "action": "voice_stream_chunk", "data_base64": "YWJj"})
                phone.send_json({"id": "voice-ok", "action": "voice_stream_end", "duration_ms": 850})
                relayed = robot.receive_json()
                assert relayed["action"] == "voice_audio"
                assert relayed["audio_base64"] == "YWJj"
                assert relayed["mime_type"] == "audio/webm"
                assert relayed["duration_ms"] == 850
                assert phone.receive_json()["status"] == "forwarded"

                phone.send_json({"id": "voice-empty", "action": "voice_stream_start", "mime_type": "audio/webm"})
                assert phone.receive_json()["ok"] is True
                phone.send_json({"id": "voice-empty", "action": "voice_stream_end", "duration_ms": 0})
                rejected = phone.receive_json()
                assert rejected["ok"] is False
                assert rejected["error"] == "empty_voice_stream"


def test_composite_scene_status_is_relayed_without_breaking_old_task_fields(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    task = {
        "active": True,
        "planning": False,
        "queued": 0,
        "active_skills": ["push_up"],
        "active_procedures": ["push_up_companion"],
    }
    with client:
        with client.websocket_connect("/ws/robot?token=robot-test-token") as robot:
            robot.send_json({"type": "telemetry", "pose": None, "task": task})
            with client.websocket_connect("/ws/app?token=app-test-token") as phone:
                state = phone.receive_json()
                assert state["task"] == task


def test_pet_and_fitness_video_feedback_contract(tmp_path, monkeypatch):
    client, module = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    with client:
        pet = client.post(
            "/api/robot/videos",
            content=b"pet-video",
            headers={
                "x-robot-token": "robot-test-token",
                "x-video-name": "pet.mp4",
                "x-video-category": "pet",
                "x-video-duration": "5",
            },
        )
        assert pet.status_code == 200
        assert pet.json()["video"]["category"] == "pet"
        assert pet.json()["video"]["duration_sec"] == 5.0

        fitness = client.post(
            "/api/robot/videos",
            content=b"fitness-video",
            headers={
                "x-robot-token": "robot-test-token",
                "x-video-name": "fitness.mp4",
                "x-video-category": "fitness",
                "x-video-exercise": "push_up",
                "x-video-exercise-label": "%E4%BF%AF%E5%8D%A7%E6%92%91",
                "x-video-count": "9",
                "x-video-identity": "anonymous",
                "x-video-session-state": "completed",
            },
        )
        assert fitness.status_code == 200
        item = fitness.json()["video"]
        assert item["category"] == "fitness"
        assert item["exercise"] == "push_up"
        assert item["count"] == 9
        assert item["identity"] == "anonymous"
        listed = client.get("/api/videos", params={"token": "app-test-token"}).json()["videos"]
        assert [entry["category"] for entry in listed] == ["fitness", "pet"]
