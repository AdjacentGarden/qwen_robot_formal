from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Iterator

from .config import runtime_dir


RESOURCE_BY_SKILL: dict[str, list[str]] = {
    "audio_record": ["mic"],
    "audio_play": ["speaker"],
    "front_camera_capture": ["front_camera"],
    "front_camera_record": ["front_camera"],
    "back_camera_capture": ["back_camera"],
    "back_camera_record": ["back_camera"],
    "camera_capture": ["camera"],
    "camera_record": ["camera"],
    "environment_perception": ["front_camera", "npu"],
    "face_recognition": ["front_camera", "npu", "face_db"],
    "face_registration": ["front_camera", "npu", "face_db"],
    "head_control": ["head_motor"],
    "light_control": ["network", "mijia_cloud", "mijia_floor_lamp"],
    "feeder_control": ["network", "mijia_cloud", "mijia_pet_feeder"],
    "move_backward": ["base"],
    "move_forward": ["base"],
    "move_left": ["base"],
    "move_right": ["base"],
    "navigation_goto": ["base", "navigation"],
    "navigation_list": [],
    "person_tracking": ["front_camera", "npu", "base"],
    "pet_tracking": ["front_camera", "npu", "base"],
    "projector_control": ["projector_i2c", "android_container", "projection_video"],
    "pull_up": ["back_camera", "npu"],
    "push_up": ["back_camera", "npu"],
    "reminder_cancel": ["reminder_store"],
    "reminder_query": ["reminder_store"],
    "reminder_schedule": ["reminder_store"],
    "squat": ["back_camera", "npu"],
}


class ResourceManager:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.lock_dir = runtime_dir(config) / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def resources_for_skill(self, skill_name: str, arguments: dict[str, Any] | None = None) -> list[str]:
        args = arguments or {}
        if skill_name in {"camera_capture", "camera_record"}:
            camera_name = args.get("camera_name") or args.get("camera") or "front"
            return [f"{camera_name}_camera"] if camera_name in {"front", "back"} else ["camera"]
        if skill_name == "environment_perception":
            camera_name = str(args.get("camera") or args.get("camera_name") or "front").lower()
            if camera_name == "both":
                return ["front_camera", "back_camera", "npu"]
            if camera_name in {"front", "back"}:
                return [f"{camera_name}_camera", "npu"]
            return ["front_camera", "npu"]
        if skill_name == "pet_tracking" and str(args.get("action") or "").lower() == "find_route":
            return ["front_camera", "npu", "base", "navigation"]
        return list(RESOURCE_BY_SKILL.get(skill_name, []))

    @contextlib.contextmanager
    def acquire(self, resources: list[str]) -> Iterator[None]:
        locks = []
        try:
            for resource in sorted(set(resources)):
                path = self.lock_dir / f"{resource}.lock"
                fh = path.open("w", encoding="utf-8")
                self._lock_file(fh)
                locks.append(fh)
            yield
        finally:
            for fh in reversed(locks):
                self._unlock_file(fh)
                fh.close()

    def _lock_file(self, fh: Any) -> None:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            return

    def _unlock_file(self, fh: Any) -> None:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except ImportError:
            return
