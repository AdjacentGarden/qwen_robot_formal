from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
PET_DIR = ROOT / "robot_skills" / "pet_tracking"
sys.path.insert(0, str(PET_DIR))

from centering import PetCenteringController  # noqa: E402


def test_off_center_target_keeps_turning_towards_the_box():
    controller = PetCenteringController(center_tolerance_ratio=0.08)

    left = controller.observe(80, 640)
    right = controller.observe(560, 640)

    assert left.centered is False
    assert left.speed_right > 0 and left.speed_left < 0
    assert right.centered is False
    assert right.speed_right < 0 and right.speed_left > 0


def test_three_consecutive_central_frames_are_required_before_success():
    controller = PetCenteringController(
        center_tolerance_ratio=0.08,
        confirmation_frames=3,
    )

    decisions = [controller.observe(center, 640) for center in (300, 322, 318)]

    assert [item.inside_center for item in decisions] == [True, True, True]
    assert [item.centered for item in decisions] == [False, False, True]
    assert all(item.speed_right == 0.0 and item.speed_left == 0.0 for item in decisions)


def test_leaving_the_center_band_resets_confirmation():
    controller = PetCenteringController(confirmation_frames=3)
    assert controller.observe(320, 640).consecutive_centered == 1
    assert controller.observe(100, 640).consecutive_centered == 0
    assert controller.observe(320, 640).consecutive_centered == 1


def _load_pet_module():
    spec = importlib.util.spec_from_file_location("pet_centering_test_runtime", PET_DIR / "run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_find_starts_video_on_first_detection_but_stops_only_after_centering(monkeypatch):
    pet = _load_pet_module()
    centers = iter((100, 140, 220, 300, 320, 325))

    class Detection:
        def __init__(self, center):
            self.rect = SimpleNamespace(
                left=center - 30,
                right=center + 30,
                top=100,
                bottom=220,
            )
            self.score = 0.9
            self.confidence = 0.9

    class Detector:
        def detect(self, _frame, _width, _height, target_classes=None):
            return [Detection(next(centers))]

    class Camera:
        def __init__(self):
            self.frame = np.zeros((480, 640, 3), dtype=np.uint8)

        def isOpened(self):
            return True

        def read(self):
            return True, self.frame.copy()

        def release(self):
            return None

    class Board:
        def __init__(self):
            self.commands = []

    board = Board()
    video_started = []
    result_payloads = []
    detector = Detector()

    monkeypatch.setattr(
        pet.runtime_models,
        "acquire_or_create",
        lambda *_args, **_kwargs: (detector, False),
    )
    monkeypatch.setattr(pet.PetTrackingSystem, "CameraReader", lambda _source: Camera())
    monkeypatch.setattr(pet.PetTrackingSystem, "_create_board", lambda: board)
    monkeypatch.setattr(pet.PetTrackingSystem, "_release_board", lambda _board: None)
    monkeypatch.setattr(pet.PetTrackingSystem, "_show_frame", lambda *_args: None)
    monkeypatch.setattr(pet.PetTrackingSystem, "_close_windows", lambda: None)
    monkeypatch.setattr(
        pet.PetTrackingSystem,
        "set_motor",
        lambda _board, speed_right, speed_left, **_kwargs: board.commands.append(
            (float(speed_right), float(speed_left))
        ),
    )
    monkeypatch.setattr(
        pet.PetTrackingSystem,
        "_write_result_payload",
        lambda payload: result_payloads.append(dict(payload)),
    )

    def record_now(_cap, first_frame, duration_sec=5.0, frame_callback=None):
        video_started.append({"duration": duration_sec, "first_frame": first_frame.copy()})
        assert callable(frame_callback)
        for _ in range(5):
            frame_callback(np.zeros((480, 640, 3), dtype=np.uint8))
        return {"status": "pending_upload", "duration_sec": duration_sec}

    monkeypatch.setattr(pet.PetTrackingSystem, "_record_found_video_for_app", record_now)

    result = pet.PetTrackingSystem.background_pet_search_task(
        "/dev/fake",
        "fake.rknn",
        "dog",
        timeout_sec=2.0,
    )

    assert len(video_started) == 1
    assert video_started[0]["duration"] == pytest.approx(5.0)
    assert result["detected"] is True
    assert result["centered"] is True
    assert result["found"] is True
    assert result["video_status"] == "pending_upload"
    # The first post-detection command still turns; stopping is allowed only
    # after the central band has been confirmed on three consecutive frames.
    assert board.commands[0] != (0.0, 0.0)
    assert any(command != (0.0, 0.0) for command in board.commands[:-1])
    assert board.commands[-1] == (0.0, 0.0)
    assert result_payloads[-1]["center_confirmation_frames"] == 3
