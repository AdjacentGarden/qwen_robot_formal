#!/usr/bin/env python3
"""Pure helpers for validating localization around a head-tilt sensor gate.

This module deliberately has no ROS dependency.  The resident runtime owns the
ROS subscriptions and feeds compact snapshots into these functions, which
makes the safety policy deterministic and unit-testable without actuating the
robot.
"""

from __future__ import annotations

import math
from typing import Any, Iterable


def motion_authorization_error(integrity: dict[str, Any] | None) -> str | None:
    state = str((integrity or {}).get("state", "ready"))
    if state == "ready":
        return None
    return (
        "localization_integrity_not_ready:"
        f"{state}:{(integrity or {}).get('reason', 'unknown')}"
    )


def circular_distance(a: float, b: float) -> float:
    return abs((float(a) - float(b) + math.pi) % (2.0 * math.pi) - math.pi)


def compact_map(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot or not snapshot.get("available"):
        return None
    resolution = float(snapshot.get("resolution", 0.0))
    width = int(snapshot.get("width", 0))
    height = int(snapshot.get("height", 0))
    origin_x = float(snapshot.get("origin_x", 0.0))
    origin_y = float(snapshot.get("origin_y", 0.0))
    if resolution <= 0.0 or width <= 0 or height <= 0:
        return None
    return {
        "width": width,
        "height": height,
        "resolution": resolution,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "min_x": origin_x,
        "min_y": origin_y,
        "max_x": origin_x + width * resolution,
        "max_y": origin_y + height * resolution,
        "stamp_sec": float(snapshot.get("stamp_sec", 0.0)),
    }


def compact_pose(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot or not snapshot.get("available"):
        return None
    required = ("x", "y", "yaw")
    if not all(key in snapshot and math.isfinite(float(snapshot[key])) for key in required):
        return None
    return {key: float(snapshot[key]) for key in required}


def compare_localization(
    baseline_pose: dict[str, Any] | None,
    current_pose: dict[str, Any] | None,
    baseline_map: dict[str, Any] | None,
    current_map: dict[str, Any] | None,
    *,
    maximum_pose_shift_m: float = 0.35,
    maximum_yaw_shift_rad: float = 0.45,
    maximum_map_edge_shift_m: float = 0.60,
) -> dict[str, Any]:
    """Compare post-recovery localization to the stationary pre-tilt baseline."""

    errors: list[str] = []
    metrics: dict[str, Any] = {}
    if baseline_pose is not None:
        if current_pose is None:
            errors.append("post_recovery_pose_unavailable")
        else:
            translation = math.hypot(
                float(current_pose["x"]) - float(baseline_pose["x"]),
                float(current_pose["y"]) - float(baseline_pose["y"]),
            )
            yaw = circular_distance(float(current_pose["yaw"]), float(baseline_pose["yaw"]))
            metrics.update({"pose_shift_m": translation, "yaw_shift_rad": yaw})
            if translation > max(0.01, float(maximum_pose_shift_m)):
                errors.append("post_recovery_pose_shift")
            if yaw > max(0.01, float(maximum_yaw_shift_rad)):
                errors.append("post_recovery_yaw_shift")

    if baseline_map is not None:
        if current_map is None:
            errors.append("post_recovery_map_unavailable")
        else:
            edge_shifts = {
                edge: abs(float(current_map[edge]) - float(baseline_map[edge]))
                for edge in ("min_x", "min_y", "max_x", "max_y")
            }
            maximum_edge_shift = max(edge_shifts.values())
            metrics.update({
                "map_edge_shifts_m": edge_shifts,
                "maximum_map_edge_shift_m": maximum_edge_shift,
            })
            if maximum_edge_shift > max(0.05, float(maximum_map_edge_shift_m)):
                errors.append("post_recovery_map_bounds_jump")

    return {"ok": not errors, "errors": errors, "metrics": metrics}


def evaluate_scan_window(
    samples: Iterable[dict[str, Any]],
    reference: dict[str, Any] | None,
    *,
    minimum_samples: int = 6,
    maximum_median_frame_delta_m: float = 0.25,
    maximum_reference_median_delta_m: float = 0.80,
    minimum_reference_overlap_ratio: float = 0.03,
) -> dict[str, Any]:
    """Validate fresh, stable level scans before reopening Cartographer.

    Invalid ranges are represented as ``None``.  Medians make the comparison
    insensitive to a person walking through a small part of the scan while a
    tilted floor scan, an empty scan, or a radically different profile is
    rejected.
    """

    items = [sample for sample in samples if bool(sample.get("valid"))]
    if len(items) < max(1, int(minimum_samples)):
        return {
            "ok": False,
            "error": "insufficient_valid_level_scans",
            "sample_count": len(items),
        }

    def paired_deltas(left: dict[str, Any], right: dict[str, Any]) -> list[float]:
        lvalues = left.get("ranges") or []
        rvalues = right.get("ranges") or []
        return [
            abs(float(a) - float(b))
            for a, b in zip(lvalues, rvalues)
            if a is not None and b is not None
        ]

    def median(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        middle = len(ordered) // 2
        return (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2.0
        )

    frame_medians: list[float] = []
    for previous, current in zip(items, items[1:]):
        value = median(paired_deltas(previous, current))
        if value is not None:
            frame_medians.append(value)
    window_delta = median(frame_medians)
    if window_delta is None or window_delta > float(maximum_median_frame_delta_m):
        return {
            "ok": False,
            "error": "level_scan_window_unstable",
            "sample_count": len(items),
            "median_frame_delta_m": window_delta,
        }

    result: dict[str, Any] = {
        "ok": True,
        "sample_count": len(items),
        "median_frame_delta_m": window_delta,
        "last_sequence": int(items[-1].get("sequence", 0)),
    }
    if reference and reference.get("valid"):
        latest = items[-1]
        deltas = paired_deltas(reference, latest)
        count = max(len(reference.get("ranges") or []), len(latest.get("ranges") or []), 1)
        overlap_ratio = len(deltas) / count
        reference_delta = median(deltas)
        result.update({
            "reference_overlap_ratio": overlap_ratio,
            "reference_median_delta_m": reference_delta,
        })
        if (
            overlap_ratio < float(minimum_reference_overlap_ratio)
            or reference_delta is None
            or reference_delta > float(maximum_reference_median_delta_m)
        ):
            return {**result, "ok": False, "error": "level_scan_differs_from_baseline"}
    return result
