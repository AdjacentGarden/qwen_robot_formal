#!/usr/bin/env python3
"""Resident orchestration for autonomous projection and map-covering pet search."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from exploration_core import (
    GridMap,
    analyze_projection_frame,
    grid_from_snapshot,
    load_map_yaml,
    modelscope_projection_judge,
    pucoding_projection_judge,
    projection_candidates,
    rectangular_search_regions,
    save_exploration_overview,
)


def _last_json(text: str) -> dict[str, Any]:
    for line in reversed(str(text or "").splitlines()):
        try:
            value = json.loads(line.strip())
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


class AutonomyEngine:
    def __init__(self, owner: Any, config_path: Path) -> None:
        self.owner = owner
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.runtime_dir = self.config_path.parent.parent / "runtime" / "exploration"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._last_head_feedback: dict[str, Any] = {}

    @staticmethod
    def _write_report(path: Path, report: dict[str, Any]) -> None:
        """Telemetry must never turn a completed physical action into failure."""
        try:
            path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            report.setdefault("warnings", []).append(
                f"report_write_failed:{type(exc).__name__}:{exc}"
            )

    def _map(
        self,
        fallback: str | None = None,
        *,
        allow_fallback: bool = True,
    ) -> GridMap:
        snapshot = self.owner.runtime.occupancy_grid_snapshot(max_age_sec=8.0)
        if snapshot.get("available"):
            return grid_from_snapshot(snapshot)
        if not allow_fallback:
            raise RuntimeError(
                f"live_map_unavailable:{snapshot.get('error', 'no_live_map')}"
            )
        path = fallback or str(self.config.get("map", {}).get("fallback_yaml") or "")
        if not path:
            raise RuntimeError(f"map_unavailable:{snapshot.get('error', 'no_fallback')}")
        return load_map_yaml(path)

    def _pose(
        self,
        grid: GridMap,
        x: float | None = None,
        y: float | None = None,
        *,
        allow_offline: bool = True,
    ) -> dict[str, Any]:
        if x is not None and y is not None:
            return {"available": True, "x": float(x), "y": float(y), "yaw": 0.0, "source": "arguments"}
        pose = self.owner.runtime.lookup_pose("map", "base_footprint")
        if pose.get("available"):
            return pose
        fallback = list(self.config.get("map", {}).get("offline_start_pose") or [0.0, 0.0, 0.0])
        if allow_offline and grid.source.startswith("yaml:"):
            return {"available": True, "x": float(fallback[0]), "y": float(fallback[1]), "yaw": float(fallback[2]), "source": "offline_config"}
        raise RuntimeError(f"current_pose_unavailable:{pose.get('error')}")

    def _head(self, action: str, angle: int | None = None) -> bool:
        argv = [action, "--skip-services", "--json", "--wait", "0.35"]
        if angle is not None:
            argv = ["angle", "--angle", str(int(angle)), "--skip-services", "--json", "--wait", "0.35"]
        return int(self.owner._head(argv)) == 0

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _move_head_closed_loop(
        self,
        cfg: dict[str, Any],
        target_angle: float,
        *,
        level: bool,
    ) -> bool:
        """Drive the head from physical feedback, compensating dead band/hysteresis."""
        prefix = "head_level" if level else "head_raise"
        target = float(target_angle)
        tolerance = float(cfg.get(f"{prefix}_target_tolerance_deg", 3.0 if level else 2.5))
        maximum_rate = float(cfg.get(
            f"{prefix}_maximum_stable_rate_dps" if level else "head_maximum_stable_rate_dps",
            2.5,
        ))
        maximum_span = float(cfg.get(
            f"{prefix}_maximum_stable_roll_span_deg" if level else "head_maximum_stable_roll_span_deg",
            0.8,
        ))
        stable_sec = float(cfg.get(
            f"{prefix}_stable_duration_sec" if level else "head_stable_duration_sec",
            0.5,
        ))
        offset_key = "head_level_command_offset_deg" if level else "head_raise_command_offset_deg"
        command = target + float(cfg.get(offset_key, 5.0))
        command_min = float(cfg.get("head_command_min_deg", 160.0))
        command_max = float(cfg.get("head_command_max_deg", 230.0))
        max_attempts = max(1, int(cfg.get("head_closed_loop_max_attempts", 4)))
        attempt_timeout = max(1.0, float(cfg.get("head_closed_loop_attempt_timeout_sec", 7.0)))
        correction_gain = max(0.1, float(cfg.get("head_closed_loop_correction_gain", 1.0)))
        maximum_correction = max(0.5, float(cfg.get("head_closed_loop_max_correction_deg", 7.0)))
        minimum_correction = max(0.0, float(cfg.get("head_closed_loop_min_correction_deg", 1.0)))
        attempts: list[dict[str, Any]] = []

        for attempt_index in range(1, max_attempts + 1):
            command = self._clamp(command, command_min, command_max)
            command_int = int(round(command))
            if not self._head("angle", command_int):
                attempts.append({
                    "attempt": attempt_index,
                    "command_deg": command_int,
                    "ok": False,
                    "error": "head_command_failed",
                })
                continue
            result = self.owner.runtime.wait_for_head_target(
                target,
                tolerance_deg=tolerance,
                maximum_rate_dps=maximum_rate,
                maximum_roll_span_deg=maximum_span,
                stable_sec=stable_sec,
                timeout_sec=attempt_timeout,
                maximum_feedback_age_sec=float(cfg.get("head_maximum_feedback_age_sec", 0.6)),
            )
            attempts.append({"attempt": attempt_index, "command_deg": command_int, **dict(result)})
            self._last_head_feedback = {
                "ok": bool(result.get("ok")),
                "target_deg": target,
                "level": bool(level),
                "attempts": attempts,
                "final": dict(result),
            }
            if result.get("ok"):
                return True

            # Position error is measured as physical_roll - target.  Correct the
            # actuator setpoint in the opposite direction.  If position is already
            # inside tolerance, simply republish and give the stability window a
            # fresh attempt instead of needlessly hunting around the target.
            error = result.get("error_deg")
            if isinstance(error, (int, float)) and math.isfinite(float(error)):
                error_value = float(error)
                if abs(error_value) > tolerance:
                    correction = self._clamp(
                        -error_value * correction_gain,
                        -maximum_correction,
                        maximum_correction,
                    )
                    if 0.0 < abs(correction) < minimum_correction:
                        correction = math.copysign(minimum_correction, correction)
                    command += correction

        self._last_head_feedback = {
            "ok": False,
            "target_deg": target,
            "level": bool(level),
            "attempts": attempts,
            "final": dict(attempts[-1]) if attempts else {"error": "head_command_not_attempted"},
        }
        return False

    def _level_head_and_wait(self, cfg: dict[str, Any]) -> bool:
        """Return only after fresh IMU feedback proves the head is level."""
        return self._move_head_closed_loop(
            cfg,
            float(cfg.get("head_level_angle", 185.0)),
            level=True,
        )

    def _raise_head_and_wait(self, cfg: dict[str, Any], angle: int) -> bool:
        """Do not allow camera capture until fresh IMU feedback proves arrival."""
        return self._move_head_closed_loop(cfg, float(angle), level=False)

    def _verify_head_before_capture(self, cfg: dict[str, Any], angle: int) -> bool:
        """Revalidate fresh physical feedback immediately before reading a frame."""
        result = self.owner.runtime.wait_for_head_target(
            float(angle),
            tolerance_deg=float(cfg.get("head_raise_target_tolerance_deg", 2.5)),
            maximum_rate_dps=float(cfg.get("head_maximum_stable_rate_dps", 2.5)),
            maximum_roll_span_deg=float(cfg.get("head_maximum_stable_roll_span_deg", 0.8)),
            stable_sec=float(cfg.get("head_capture_gate_stable_duration_sec", 0.25)),
            timeout_sec=float(cfg.get("head_capture_gate_timeout_sec", 3.0)),
            maximum_feedback_age_sec=float(cfg.get("head_maximum_feedback_age_sec", 0.6)),
        )
        self._last_head_feedback = {
            **dict(self._last_head_feedback),
            "capture_gate": dict(result),
        }
        return bool(result.get("ok"))

    def _capture_front(self, destination: Path, settle_sec: float) -> np.ndarray:
        if settle_sec > 0:
            time.sleep(settle_sec)
        cap = self.owner.camera.lease("/dev/video22", 640, 480, 15.0)
        frame = None
        for _ in range(5):
            ok, value = cap.read()
            if ok and value is not None:
                frame = value
            time.sleep(0.03)
        if frame is None:
            raise RuntimeError("front_camera_frame_unavailable")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(destination), frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            raise RuntimeError(f"camera_image_write_failed:{destination}")
        return frame

    def _start_meeting_projection(self) -> bool:
        return int(self.owner._generic("projector_control", ["meeting_presentation_on", "--json"])) == 0

    def projection_cli(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser(description="Autonomous projection-surface exploration")
        parser.add_argument("action", nargs="?", choices=["search_and_project", "plan", "stop"], default="search_and_project")
        parser.add_argument("--map-yaml")
        parser.add_argument("--start-x", type=float)
        parser.add_argument("--start-y", type=float)
        parser.add_argument("--max-candidates", type=int)
        parser.add_argument(
            "--judge",
            choices=["auto", "local", "vlm", "modelscope", "pucoding"],
            default="auto",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--allow-offline-execution", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv)
        if args.action == "stop":
            self.owner.exploration_stop.set()
            self.owner.runtime.navigation_cancel_request()
            with contextlib.suppress(Exception):
                self.owner.runtime.chassis_stop()
            print(json.dumps({"ok": True, "skill": "autonomous_projection", "action": "stop", "status": "stop_requested"}, ensure_ascii=False))
            return 0

        self.owner.exploration_stop.clear()
        started = time.monotonic()
        cfg = dict(self.config["projection"])
        hardware_mode = not bool(args.dry_run or args.action == "plan")
        if args.max_candidates is not None:
            cfg["maximum_candidates"] = max(1, int(args.max_candidates))
        try:
            offline_mode = bool(
                args.dry_run
                or args.action == "plan"
                or args.allow_offline_execution
            )
            grid = self._map(args.map_yaml, allow_fallback=offline_mode)
            pose = self._pose(
                grid,
                args.start_x,
                args.start_y,
                allow_offline=offline_mode,
            )
            candidates = projection_candidates(grid, (pose["x"], pose["y"]), cfg)
            session = self.runtime_dir / f"projection_{int(time.time())}"
            session.mkdir(parents=True, exist_ok=True)
            save_exploration_overview(grid, session / "candidate_overview.png", candidates=candidates)
            report: dict[str, Any] = {
                "map": grid.to_json_summary(), "start_pose": pose,
                "candidate_count": len(candidates), "candidates": [item.public() for item in candidates],
                "attempts": [], "projection_footprint_calibration": cfg.get("camera_projector_calibration"),
                "session_dir": str(session),
            }
            if args.dry_run or args.action == "plan":
                report.update({"ok": bool(candidates), "status": "planned", "dry_run": True, "suitable": None})
                self._write_report(session / "report.json", report)
                print(json.dumps({"ok": bool(candidates), "skill": "autonomous_projection", "action": args.action, **report}, ensure_ascii=False))
                return 0 if candidates else 1
            if not candidates:
                report.update({"ok": False, "status": "no_reachable_wall_candidates", "suitable": False})
                print(json.dumps({"skill": "autonomous_projection", "action": args.action, **report}, ensure_ascii=False))
                return 1

            angles = [int(value) for value in cfg.get("head_scan_angles", [211])]
            vlm_attempts = 0
            vlm_successes = 0
            vlm_failures = 0
            for index, candidate in enumerate(candidates, 1):
                if self.owner.exploration_stop.is_set():
                    report.update({"ok": False, "status": "cancelled", "suitable": False})
                    break
                # Strict motion interlock: never issue a navigation goal while the
                # head may still be returning from the previous camera angle.  This
                # also protects the first candidate if a prior task left it raised.
                if not self._level_head_and_wait(cfg):
                    report.update({
                        "ok": False,
                        "status": "head_level_failed_before_navigation",
                        "suitable": False,
                        "failed_candidate": candidate.public(),
                        "head_feedback": dict(self._last_head_feedback),
                    })
                    break
                navigation = self.owner.runtime.navigation_goal({
                    "x": candidate.x, "y": candidate.y, "yaw": candidate.yaw, "frame_id": "map",
                    "timeout": float(cfg.get("navigation_timeout_sec", 120.0)),
                    "server_wait_timeout": 5.0, "goal_response_timeout": 8.0,
                })
                attempt: dict[str, Any] = {"candidate": candidate.public(), "navigation": navigation, "angles": []}
                report["attempts"].append(attempt)
                if navigation.get("status") != "succeeded":
                    attempt["status"] = "navigation_failed"
                    continue
                for angle in angles:
                    if self.owner.exploration_stop.is_set():
                        break
                    # A successful ROS publish is not motor-position feedback.  The
                    # IMU interlock must prove physical arrival before capture.
                    if not self._raise_head_and_wait(cfg, angle):
                        attempt["angles"].append({
                            "angle": angle,
                            "status": "head_failed",
                            "head_feedback": dict(self._last_head_feedback),
                        })
                        continue
                    raw_path = session / f"candidate_{index:02d}_angle_{angle}_raw.jpg"
                    annotated_path = session / f"candidate_{index:02d}_angle_{angle}_footprint.jpg"
                    try:
                        settle_sec = float(cfg.get("camera_settle_sec", 0.15))
                        if settle_sec > 0:
                            time.sleep(settle_sec)
                        # The camera is gated a second time immediately before the
                        # first frame read.  If the head drifted during settling,
                        # run one more closed-loop correction rather than capturing
                        # at an unverified angle.
                        if not self._verify_head_before_capture(cfg, angle):
                            if not self._raise_head_and_wait(cfg, angle) or not self._verify_head_before_capture(cfg, angle):
                                attempt["angles"].append({
                                    "angle": angle,
                                    "status": "head_capture_gate_failed",
                                    "head_feedback": dict(self._last_head_feedback),
                                })
                                continue
                        capture_feedback = dict(self._last_head_feedback)
                        frame = self._capture_front(raw_path, 0.0)
                        local = analyze_projection_frame(frame, cfg, annotated_path)
                        visual: dict[str, Any] = {
                            "local": local,
                            "annotated": str(annotated_path),
                            "raw": str(raw_path),
                            "head_feedback_at_capture": capture_feedback,
                        }
                        configured_provider = str(cfg.get("visual_judge_provider") or "modelscope").lower()
                        explicit_provider = (
                            "modelscope" if args.judge in {"vlm", "modelscope"}
                            else "pucoding" if args.judge == "pucoding"
                            else configured_provider
                        )
                        should_call_vlm = bool(
                            args.judge in {"vlm", "modelscope", "pucoding"}
                            or (
                                args.judge == "auto"
                                and bool(cfg.get("vlm_required", True))
                            )
                        )
                        if should_call_vlm:
                            vlm_attempts += 1
                            try:
                                if explicit_provider == "pucoding":
                                    visual["vlm"] = pucoding_projection_judge(annotated_path, local, cfg)
                                elif explicit_provider == "modelscope":
                                    visual["vlm"] = modelscope_projection_judge(annotated_path, local, cfg)
                                else:
                                    raise ValueError(f"unsupported_visual_judge_provider:{explicit_provider}")
                                vlm_successes += 1
                            except Exception as exc:
                                vlm_failures += 1
                                visual["vlm"] = {
                                    "ok": False,
                                    "provider": explicit_provider,
                                    "error": f"{type(exc).__name__}: {exc}",
                                }
                        required = bool(cfg.get("vlm_required", True)) and args.judge != "local"
                        if required and explicit_provider == "pucoding":
                            suitable = bool(visual.get("vlm", {}).get("ok"))
                        else:
                            suitable = bool(local["ok"] and (visual.get("vlm", {}).get("ok") if required else True))
                        visual["suitable"] = suitable
                        attempt["angles"].append({"angle": angle, "status": "judged", "visual": visual})
                        if not suitable:
                            continue
                        print(json.dumps({
                            "event": "projection_surface_accepted",
                            "candidate_id": candidate.id,
                            "head_angle": angle,
                            "provider": visual.get("vlm", {}).get("provider"),
                            "confidence": visual.get("vlm", {}).get("confidence"),
                        }, ensure_ascii=False), flush=True)
                        projected = self._start_meeting_projection()
                        attempt["status"] = "projecting" if projected else "projector_failed"
                        report.update({
                            "ok": projected, "status": attempt["status"], "suitable": True,
                            "selected_candidate": candidate.public(), "selected_head_angle": angle,
                            "elapsed_sec": round(time.monotonic() - started, 3),
                        })
                        self._write_report(session / "report.json", report)
                        print(json.dumps({"skill": "autonomous_projection", "action": args.action, **report}, ensure_ascii=False))
                        if not projected:
                            self._level_head_and_wait(cfg)
                        return 0 if projected else 1
                    except Exception as exc:
                        attempt["angles"].append({"angle": angle, "status": "capture_or_judge_failed", "error": f"{type(exc).__name__}: {exc}"})
                attempt.setdefault("status", "surface_unsuitable")
            self._level_head_and_wait(cfg)
            report.setdefault("ok", False)
            if vlm_attempts and not vlm_successes and vlm_failures:
                report.setdefault("status", "projection_judge_unavailable")
                report.setdefault(
                    "error",
                    f"all_vlm_requests_failed:{vlm_failures}",
                )
            else:
                report.setdefault("status", "no_suitable_projection_surface")
            report.setdefault("suitable", False)
            report["vlm_attempts"] = vlm_attempts
            report["vlm_successes"] = vlm_successes
            report["vlm_failures"] = vlm_failures
            report["elapsed_sec"] = round(time.monotonic() - started, 3)
            self._write_report(session / "report.json", report)
            print(json.dumps({"skill": "autonomous_projection", "action": args.action, **report}, ensure_ascii=False))
            return 1
        except Exception as exc:
            with contextlib.suppress(Exception):
                if not args.dry_run:
                    self._level_head_and_wait(cfg)
            print(json.dumps({
                "ok": False, "skill": "autonomous_projection", "action": args.action,
                "status": "error", "suitable": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_sec": round(time.monotonic() - started, 3),
            }, ensure_ascii=False))
            return 1
        finally:
            if hardware_mode:
                with contextlib.suppress(Exception):
                    self.owner.runtime.chassis_stop()

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> bytes:
        chunks = []
        while size:
            block = conn.recv(size)
            if not block:
                raise ConnectionError("pet_worker_socket_closed")
            chunks.append(block)
            size -= len(block)
        return b"".join(chunks)

    def _pet_worker(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        path = self.config_path.parent.parent / "runtime" / "resident" / "pet.sock"
        header = {**request, "payload_len": 0}
        encoded = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(path))
            conn.sendall(struct.pack("!I", len(encoded)) + encoded)
            size = struct.unpack("!I", self._recv_exact(conn, 4))[0]
            return json.loads(self._recv_exact(conn, size).decode("utf-8"))

    def _scan_pet(self, cfg: dict[str, Any], direction: int, index: int) -> dict[str, Any]:
        return self._pet_worker({
            "op": "pet_scan360", "pet": str(cfg.get("pet", "dog")),
            "source": str(cfg.get("camera", "/dev/video22")),
            "angular_speed": abs(float(cfg.get("scan_angular_speed", 0.20))) * (1 if direction >= 0 else -1),
            "revolutions": 1.0, "timeout_sec": float(cfg.get("scan_timeout_sec", 42.0)),
            "minimum_confirmations": int(cfg.get("minimum_confirmations", 2)),
            "output": str(self.runtime_dir / f"pet_scan_{int(time.time())}_{index:02d}.jpg"),
        }, timeout=float(cfg.get("scan_timeout_sec", 42.0)) + 8.0)

    def pet_search_cli(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser(description="Map-covering pet search")
        parser.add_argument("action", nargs="?", choices=["search", "plan", "stop"], default="search")
        parser.add_argument("--pet", choices=["dog", "cat"], default="dog")
        parser.add_argument("--map-yaml")
        parser.add_argument("--start-x", type=float)
        parser.add_argument("--start-y", type=float)
        parser.add_argument("--max-regions", "--max-viewpoints", dest="max_regions", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--allow-offline-execution", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv)
        if args.action == "stop":
            self.owner.exploration_stop.set()
            self.owner.runtime.navigation_cancel_request()
            with contextlib.suppress(Exception):
                self._pet_worker({"op": "pet_stop_scan"}, timeout=3.0)
            self.owner.runtime.chassis_stop()
            print(json.dumps({"ok": True, "skill": "pet_map_search", "action": "stop", "status": "stop_requested"}, ensure_ascii=False))
            return 0

        self.owner.exploration_stop.clear()
        started = time.monotonic()
        cfg = dict(self.config["pet_search"])
        cfg["pet"] = args.pet
        if args.max_regions is not None:
            cfg["maximum_regions"] = max(1, int(args.max_regions))
        session = self.runtime_dir / f"pet_search_{int(time.time())}"
        session.mkdir(parents=True, exist_ok=True)
        hardware_mode = not bool(args.dry_run or args.action == "plan")
        allow_offline = bool(not hardware_mode or args.allow_offline_execution)
        try:
            grid = self._map(args.map_yaml, allow_fallback=allow_offline)
            pose = self._pose(
                grid,
                args.start_x,
                args.start_y,
                allow_offline=allow_offline,
            )
            current = grid.world_to_cell(pose["x"], pose["y"])
            regions = rectangular_search_regions(grid, current, cfg)
            scans: list[dict[str, Any]] = []
            failed_regions: list[dict[str, Any]] = []
            visited_regions: list[dict[str, Any]] = []

            if not regions:
                raise RuntimeError("no_reachable_rectangular_search_regions")
            distance_limited_regions = [item.public() for item in regions if not item.within_detection_distance]

            if args.dry_run or args.action == "plan":
                save_exploration_overview(grid, session / "rectangular_region_plan.png", regions=regions)
                report = {
                    "ok": bool(regions) and not distance_limited_regions,
                    "skill": "pet_map_search", "action": args.action,
                    "status": "planned" if not distance_limited_regions else "partition_distance_limit_unmet",
                    "dry_run": True, "found": None,
                    "partition_mode": "few_rectangular_regions",
                    "region_count": len(regions), "regions": [item.public() for item in regions],
                    "distance_limited_regions": distance_limited_regions,
                    "map": grid.to_json_summary(), "session_dir": str(session),
                }
                self._write_report(session / "report.json", report)
                print(json.dumps(report, ensure_ascii=False))
                return 0 if report["ok"] else 1

            if distance_limited_regions:
                raise RuntimeError("rectangular_partition_exceeds_pet_detection_distance")

            arrival_tolerance = max(0.05, float(cfg.get("center_arrival_tolerance_m", 0.20)))
            for scan_index, region in enumerate(regions):
                if self.owner.exploration_stop.is_set():
                    raise RuntimeError("pet_search_cancelled")
                pose_now = self.owner.runtime.lookup_pose("map", "base_footprint")
                if pose_now.get("available"):
                    distance_to_center = float(math.hypot(region.x - pose_now["x"], region.y - pose_now["y"]))
                elif scan_index == 0:
                    distance_to_center = float(math.hypot(region.x - pose["x"], region.y - pose["y"]))
                else:
                    distance_to_center = float("inf")
                if distance_to_center <= arrival_tolerance:
                    navigation = {"ok": True, "status": "already_at_region_center", "distance_m": distance_to_center}
                else:
                    navigation = self.owner.runtime.navigation_goal({
                        "x": region.x, "y": region.y, "yaw": 0.0, "frame_id": "map",
                        "timeout": float(cfg.get("navigation_timeout_sec", 120.0)),
                        "server_wait_timeout": 5.0, "goal_response_timeout": 8.0,
                    })
                region.navigation = navigation
                if navigation.get("status") not in {"succeeded", "already_at_region_center"}:
                    failed_regions.append(region.public())
                    continue
                visited_regions.append(region.public())
                direction = 1 if scan_index % 2 == 0 else -1
                scan = self._scan_pet(cfg, direction, scan_index)
                scans.append(scan)
                if scan.get("found"):
                    report = {
                        "ok": True, "skill": "pet_map_search", "action": args.action,
                        "status": "found", "found": True, "scan_count": len(scans),
                        "partition_mode": "few_rectangular_regions",
                        "region_count": len(regions), "visited_regions": visited_regions,
                        "failed_regions": failed_regions, "scans": scans,
                        "session_dir": str(session), "elapsed_sec": round(time.monotonic() - started, 3),
                    }
                    self._write_report(session / "report.json", report)
                    print(json.dumps(report, ensure_ascii=False))
                    return 0
                if not scan.get("ok"):
                    raise RuntimeError(f"pet_scan_failed:{scan.get('error') or scan.get('status')}")
            save_exploration_overview(grid, session / "rectangular_region_result.png", regions=regions)
            if failed_regions:
                report = {
                    "ok": False, "skill": "pet_map_search", "action": args.action,
                    "status": "incomplete", "found": False, "exhausted": False,
                    "partition_mode": "few_rectangular_regions",
                    "region_count": len(regions), "visited_regions": visited_regions,
                    "failed_regions": failed_regions, "scan_count": len(scans), "scans": scans,
                    "error": "one_or_more_region_centers_unreachable",
                    "session_dir": str(session), "elapsed_sec": round(time.monotonic() - started, 3),
                }
                self._write_report(session / "report.json", report)
                print(json.dumps(report, ensure_ascii=False))
                return 1
            report = {
                "ok": True, "skill": "pet_map_search", "action": args.action,
                "status": "not_found", "found": False, "exhausted": True,
                "partition_mode": "few_rectangular_regions",
                "region_count": len(regions), "visited_regions": visited_regions,
                "failed_regions": [], "scan_count": len(scans), "scans": scans,
                "session_dir": str(session), "elapsed_sec": round(time.monotonic() - started, 3),
            }
            self._write_report(session / "report.json", report)
            print(json.dumps(report, ensure_ascii=False))
            return 0
        except Exception as exc:
            report = {
                "ok": False, "skill": "pet_map_search", "action": args.action,
                "status": "cancelled" if "cancelled" in str(exc) else "incomplete",
                "found": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_sec": round(time.monotonic() - started, 3), "session_dir": str(session),
            }
            self._write_report(session / "report.json", report)
            print(json.dumps(report, ensure_ascii=False))
            return 1
        finally:
            if hardware_mode:
                with contextlib.suppress(Exception):
                    self.owner.runtime.chassis_stop()
