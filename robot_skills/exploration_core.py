#!/usr/bin/env python3
"""Pure map, projection-footprint and visual-coverage algorithms.

This module intentionally has no ROS or robot hardware imports.  The resident
runtime supplies snapshots and executes the resulting poses.  Keeping geometry
pure makes the dangerous parts of autonomous exploration testable offline.
"""

from __future__ import annotations

import base64
import heapq
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np


NEIGHBORS = (
    (1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
    (1, 1, math.sqrt(2.0)), (1, -1, math.sqrt(2.0)),
    (-1, 1, math.sqrt(2.0)), (-1, -1, math.sqrt(2.0)),
)


def wrap_angle(value: float) -> float:
    return (float(value) + math.pi) % (2.0 * math.pi) - math.pi


@dataclass
class GridMap:
    data: np.ndarray
    resolution: float
    origin_x: float
    origin_y: float
    frame_id: str = "map"
    stamp_sec: float = 0.0
    source: str = "unknown"

    @property
    def height(self) -> int:
        return int(self.data.shape[0])

    @property
    def width(self) -> int:
        return int(self.data.shape[1])

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        col = int(math.floor((float(x) - self.origin_x) / self.resolution))
        row = int(math.floor((float(y) - self.origin_y) / self.resolution))
        return row, col

    def cell_to_world(self, cell: tuple[int, int]) -> tuple[float, float]:
        row, col = cell
        return (
            self.origin_x + (float(col) + 0.5) * self.resolution,
            self.origin_y + (float(row) + 0.5) * self.resolution,
        )

    def valid(self, cell: tuple[int, int]) -> bool:
        row, col = cell
        return 0 <= row < self.height and 0 <= col < self.width

    def free_mask(self, threshold: int = 25) -> np.ndarray:
        return (self.data >= 0) & (self.data <= int(threshold))

    def occupied_mask(self, threshold: int = 65) -> np.ndarray:
        return self.data >= int(threshold)

    def known_mask(self) -> np.ndarray:
        return self.data >= 0

    def clearance_m(self, occupied_threshold: int = 65) -> np.ndarray:
        traversable = ((self.data >= 0) & (self.data < occupied_threshold)).astype(np.uint8)
        return cv2.distanceTransform(traversable, cv2.DIST_L2, 5) * self.resolution

    def to_json_summary(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "origin": [self.origin_x, self.origin_y],
            "frame_id": self.frame_id,
            "stamp_sec": self.stamp_sec,
            "source": self.source,
            "free_cells": int(np.count_nonzero(self.free_mask())),
            "occupied_cells": int(np.count_nonzero(self.occupied_mask())),
            "unknown_cells": int(np.count_nonzero(self.data < 0)),
        }


@dataclass
class WallCandidate:
    id: str
    wall_id: str
    x: float
    y: float
    yaw: float
    target_x: float
    target_y: float
    wall_length_m: float
    wall_distance_m: float
    clearance_m: float
    normal_error_deg: float
    center_offset_m: float = 0.0
    path_length_m: float = 0.0
    path_min_clearance_m: float = 0.0
    path_cells: list[tuple[int, int]] = field(default_factory=list, repr=False)

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("path_cells", None)
        return value


@dataclass
class SearchViewpoint:
    x: float
    y: float
    yaw: float
    gain_cells: int
    path_length_m: float
    coverage_after: float
    path_cells: list[tuple[int, int]] = field(default_factory=list, repr=False)
    navigation: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("path_cells", None)
        return value


@dataclass
class RectangularSearchRegion:
    id: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    geometric_center_x: float
    geometric_center_y: float
    x: float
    y: float
    center_offset_m: float
    maximum_corner_distance_m: float
    within_detection_distance: bool
    area_m2: float
    fill_ratio: float
    path_length_m: float = 0.0
    path_cells: list[tuple[int, int]] = field(default_factory=list, repr=False)
    navigation: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("path_cells", None)
        return value


def grid_from_snapshot(snapshot: dict[str, Any]) -> GridMap:
    data = np.asarray(snapshot["data"], dtype=np.int16)
    height = int(snapshot["height"])
    width = int(snapshot["width"])
    if data.size != height * width:
        raise ValueError("occupancy_snapshot_shape_mismatch")
    return GridMap(
        data=data.reshape((height, width)),
        resolution=float(snapshot["resolution"]),
        origin_x=float(snapshot["origin_x"]),
        origin_y=float(snapshot["origin_y"]),
        frame_id=str(snapshot.get("frame_id") or "map"),
        stamp_sec=float(snapshot.get("stamp_sec") or 0.0),
        source=str(snapshot.get("source") or "ros:/map"),
    )


def _read_simple_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        output: dict[str, Any] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = [item.strip() for item in line.split(":", 1)]
            if value.startswith("["):
                output[key] = json.loads(value)
            else:
                output[key] = value.strip("\"'")
        return output


def load_map_yaml(path: str | Path) -> GridMap:
    yaml_path = Path(path).expanduser().resolve()
    meta = _read_simple_yaml(yaml_path)
    image_path = Path(str(meta.get("image") or ""))
    if not image_path.is_absolute():
        image_path = yaml_path.parent / image_path
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise FileNotFoundError(f"map_image_unreadable:{image_path}")
    image = np.flipud(image)
    negate = int(meta.get("negate", 0) or 0)
    values = image.astype(np.float32) / 255.0
    probability = values if negate else 1.0 - values
    occupied_threshold = float(meta.get("occupied_thresh", 0.65) or 0.65)
    free_threshold = float(meta.get("free_thresh", 0.196) or 0.196)
    grid = np.full(image.shape, -1, np.int16)
    grid[probability > occupied_threshold] = 100
    grid[probability < free_threshold] = 0
    origin = list(meta.get("origin") or [0.0, 0.0, 0.0])
    return GridMap(
        data=grid,
        resolution=float(meta.get("resolution") or 0.05),
        origin_x=float(origin[0]),
        origin_y=float(origin[1]),
        frame_id="map",
        source=f"yaml:{yaml_path}",
    )


def snap_free(grid: GridMap, cell: tuple[int, int], clearance_m: float, max_radius: int = 30) -> tuple[int, int] | None:
    clearance = grid.clearance_m()
    free = grid.free_mask()
    row0, col0 = cell
    for radius in range(max(0, max_radius) + 1):
        options: list[tuple[float, int, int]] = []
        for row in range(max(0, row0 - radius), min(grid.height, row0 + radius + 1)):
            for col in range(max(0, col0 - radius), min(grid.width, col0 + radius + 1)):
                if free[row, col] and clearance[row, col] >= clearance_m:
                    options.append(((row - row0) ** 2 + (col - col0) ** 2, row, col))
        if options:
            _, row, col = min(options)
            return row, col
    return None


def astar(grid: GridMap, start: tuple[int, int], goal: tuple[int, int], clearance_m: float) -> list[tuple[int, int]] | None:
    clear = grid.free_mask() & (grid.clearance_m() >= float(clearance_m))
    start = snap_free(grid, start, clearance_m)
    goal = snap_free(grid, goal, clearance_m)
    if start is None or goal is None:
        return None
    queue: list[tuple[float, tuple[int, int]]] = [(0.0, start)]
    came: dict[tuple[int, int], tuple[int, int]] = {}
    cost: dict[tuple[int, int], float] = {start: 0.0}
    while queue:
        _, current = heapq.heappop(queue)
        if current == goal:
            path = [current]
            while current in came:
                current = came[current]
                path.append(current)
            return list(reversed(path))
        for dr, dc, step in NEIGHBORS:
            nxt = current[0] + dr, current[1] + dc
            if not grid.valid(nxt) or not clear[nxt]:
                continue
            new_cost = cost[current] + step
            if new_cost >= cost.get(nxt, float("inf")):
                continue
            cost[nxt] = new_cost
            came[nxt] = current
            heuristic = math.hypot(goal[0] - nxt[0], goal[1] - nxt[1])
            heapq.heappush(queue, (new_cost + heuristic, nxt))
    return None


def reachable_mask(grid: GridMap, start: tuple[int, int], clearance_m: float) -> np.ndarray:
    clear = grid.free_mask() & (grid.clearance_m() >= float(clearance_m))
    start = snap_free(grid, start, clearance_m)
    reached = np.zeros_like(clear, dtype=np.uint8)
    if start is None:
        return reached.astype(bool)
    queue = [start]
    reached[start] = 1
    index = 0
    while index < len(queue):
        row, col = queue[index]
        index += 1
        for dr, dc, _ in NEIGHBORS[:4]:
            nxt = row + dr, col + dc
            if grid.valid(nxt) and clear[nxt] and not reached[nxt]:
                reached[nxt] = 1
                queue.append(nxt)
    return reached.astype(bool)


def _canonical_line_angle(value: float) -> float:
    value %= math.pi
    return value - math.pi if value >= math.pi / 2.0 else value


def _segment_geometry(line: tuple[int, int, int, int], grid: GridMap) -> dict[str, Any]:
    x1, y1, x2, y2 = line
    a = np.array(grid.cell_to_world((y1, x1)), dtype=float)
    b = np.array(grid.cell_to_world((y2, x2)), dtype=float)
    tangent = b - a
    length = float(np.linalg.norm(tangent))
    tangent /= max(length, 1e-9)
    if tangent[0] < 0 or (abs(tangent[0]) < 1e-9 and tangent[1] < 0):
        tangent *= -1.0
        a, b = b, a
    normal = np.array([-tangent[1], tangent[0]])
    center = (a + b) * 0.5
    return {
        "a": a, "b": b, "center": center, "tangent": tangent,
        "normal": normal, "length": length,
        "angle": _canonical_line_angle(math.atan2(tangent[1], tangent[0])),
        "offset": float(np.dot(center, normal)), "line": line,
    }


def _detect_wall_segments(grid: GridMap, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    occupied = grid.occupied_mask().astype(np.uint8) * 255
    occupied = cv2.morphologyEx(occupied, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    occupied = cv2.morphologyEx(occupied, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    # Detect short, reliable pieces first.  The final physical-wall length is
    # checked only after collinear pieces have been grouped.  Using the final
    # projector-footprint width here used to discard every broken vertical wall
    # before it had a chance to be reconstructed.
    final_minimum = float(cfg.get("minimum_wall_length_m", 1.5))
    minimum = min(
        final_minimum,
        max(grid.resolution * 8.0, float(cfg.get("hough_min_segment_length_m", 0.5))),
    )
    raw = cv2.HoughLinesP(
        occupied,
        rho=1,
        theta=np.pi / 360.0,
        threshold=max(18, int(minimum / grid.resolution * 0.45)),
        minLineLength=max(8, int(minimum / grid.resolution)),
        maxLineGap=max(3, int(float(cfg.get("hough_max_gap_m", 0.45)) / grid.resolution)),
    )
    if raw is None:
        return []
    segments = [_segment_geometry(tuple(map(int, item[0])), grid) for item in raw]
    return [item for item in segments if item["length"] >= minimum]


def _fit_wall_group(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit one physical wall to noisy, overlapping Hough line pieces."""

    points = np.asarray([point for item in group for point in (item["a"], item["b"])], dtype=float)
    center = np.mean(points, axis=0)
    centered = points - center
    covariance = centered.T @ centered
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    tangent = np.asarray(eigenvectors[:, int(np.argmax(eigenvalues))], dtype=float)
    tangent /= max(float(np.linalg.norm(tangent)), 1e-9)
    if tangent[0] < 0 or (abs(float(tangent[0])) < 1e-9 and tangent[1] < 0):
        tangent *= -1.0
    normal = np.array([-tangent[1], tangent[0]], dtype=float)
    along = centered @ tangent
    across = centered @ normal
    low, high = float(np.min(along)), float(np.max(along))
    a = center + tangent * low
    b = center + tangent * high
    return {
        "a": a,
        "b": b,
        "center": (a + b) * 0.5,
        "tangent": tangent,
        "normal": normal,
        "length": float(high - low),
        "angle": _canonical_line_angle(math.atan2(float(tangent[1]), float(tangent[0]))),
        "fit_rms_m": float(np.sqrt(np.mean(across * across))),
        "fit_max_m": float(np.max(np.abs(across))),
        "segments": len(group),
    }


def _group_wall_segments(segments: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    max_angle = math.radians(float(cfg.get("wall_group_max_angle_deg", 4.0)))
    max_perp = float(cfg.get("wall_group_max_perpendicular_distance_m", 0.28))
    max_gap = float(cfg.get("wall_group_max_along_gap_m", 0.85))
    max_fit = float(cfg.get("wall_group_max_fit_residual_m", max_perp))
    groups: list[list[dict[str, Any]]] = []
    for segment in sorted(segments, key=lambda item: -item["length"]):
        selected: list[dict[str, Any]] | None = None
        selected_score: tuple[float, float, float] | None = None
        for group in groups:
            reference = _fit_wall_group(group)
            angle_error = abs(wrap_angle(segment["angle"] - reference["angle"]))
            angle_error = min(angle_error, abs(math.pi - angle_error))
            if angle_error > max_angle:
                continue
            normal = reference["normal"]
            perpendicular = abs(float(np.dot(segment["center"] - reference["center"], normal)))
            if perpendicular > max_perp:
                continue
            tangent = reference["tangent"]
            intervals = []
            for item in group:
                values = sorted((float(np.dot(item["a"], tangent)), float(np.dot(item["b"], tangent))))
                intervals.append(values)
            new_interval = sorted((float(np.dot(segment["a"], tangent)), float(np.dot(segment["b"], tangent))))
            low, high = min(v[0] for v in intervals), max(v[1] for v in intervals)
            gap = max(0.0, low - new_interval[1], new_interval[0] - high)
            if gap > max_gap:
                continue
            trial = _fit_wall_group([*group, segment])
            if trial["fit_max_m"] > max_fit:
                continue
            score = (angle_error, perpendicular, gap)
            if selected_score is None or score < selected_score:
                selected = group
                selected_score = score
        if selected is None:
            groups.append([segment])
        else:
            selected.append(segment)
    merged: list[dict[str, Any]] = []
    for index, group in enumerate(groups, 1):
        fitted = _fit_wall_group(group)
        fitted["id"] = f"wall_{index:02d}"
        merged.append(fitted)
    return sorted(merged, key=lambda item: -item["length"])


def _line_cells(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    mask_h = max(a[0], b[0]) + 2
    mask_w = max(a[1], b[1]) + 2
    mask = np.zeros((mask_h, mask_w), np.uint8)
    cv2.line(mask, (a[1], a[0]), (b[1], b[0]), 1, 1)
    rows, cols = np.where(mask > 0)
    order = np.argsort((rows - a[0]) ** 2 + (cols - a[1]) ** 2)
    return [(int(rows[i]), int(cols[i])) for i in order]


def ray_clear_to_wall(grid: GridMap, start: tuple[int, int], end: tuple[int, int], ignore_tail_m: float = 0.20) -> bool:
    cells = _line_cells(start, end)
    ignore = max(2, int(round(ignore_tail_m / grid.resolution)))
    for cell in cells[:-ignore] if len(cells) > ignore else []:
        if not grid.valid(cell) or grid.data[cell] < 0 or grid.data[cell] >= 65:
            return False
    return True


def wall_surface_supported(
    grid: GridMap,
    target: np.ndarray,
    tangent: np.ndarray,
    width_m: float,
    cfg: dict[str, Any],
) -> bool:
    """Reject a grouped line when the intended image crosses a real opening.

    Short map dropouts are tolerated, but a doorway or a long missing strip in
    the actual projector footprint cannot be turned into a wall merely because
    the Hough pieces on both sides were grouped.
    """

    radius_m = max(grid.resolution, float(cfg.get("wall_surface_support_radius_m", 0.16)))
    minimum_ratio = float(cfg.get("wall_surface_minimum_support_ratio", 0.72))
    maximum_gap_m = max(grid.resolution, float(cfg.get("wall_surface_max_gap_m", 0.35)))
    sample_count = max(9, int(math.ceil(float(width_m) / grid.resolution)) + 1)
    offsets = np.linspace(-float(width_m) * 0.5, float(width_m) * 0.5, sample_count)
    radius_cells = max(1, int(math.ceil(radius_m / grid.resolution)))
    occupied = grid.occupied_mask()
    supported: list[bool] = []
    for offset in offsets:
        cell = grid.world_to_cell(*(target + tangent * float(offset)))
        row, col = cell
        r0, r1 = max(0, row - radius_cells), min(grid.height, row + radius_cells + 1)
        c0, c1 = max(0, col - radius_cells), min(grid.width, col + radius_cells + 1)
        supported.append(bool(r0 < r1 and c0 < c1 and np.any(occupied[r0:r1, c0:c1])))
    if not supported or float(sum(supported)) / float(len(supported)) < minimum_ratio:
        return False
    sample_step_m = float(width_m) / max(1, len(supported) - 1)
    longest_missing = 0
    current_missing = 0
    for present in supported:
        current_missing = 0 if present else current_missing + 1
        longest_missing = max(longest_missing, current_missing)
    return longest_missing * sample_step_m <= maximum_gap_m + sample_step_m * 0.5


def projection_corridor_clear(
    grid: GridMap,
    start: tuple[int, int],
    target: np.ndarray,
    tangent: np.ndarray,
    width_m: float,
    margin_m: float = 0.10,
    samples: int = 9,
) -> bool:
    """Check the whole projected fan, not only its centre ray.

    A neighbouring partition can leave the centre ray clear while clipping one
    side of a 1.67 m-wide image.  Sampling parallel target points across the
    physical footprint rejects that geometry before visual inspection.
    """

    half_width = max(0.05, float(width_m) * 0.5 + max(0.0, float(margin_m)))
    for offset in np.linspace(-half_width, half_width, max(3, int(samples))):
        endpoint = target + tangent * float(offset)
        end_cell = grid.world_to_cell(float(endpoint[0]), float(endpoint[1]))
        if not grid.valid(end_cell) or not ray_clear_to_wall(grid, start, end_cell):
            return False
    return True


def projection_candidates(grid: GridMap, start_xy: tuple[float, float], cfg: dict[str, Any]) -> list[WallCandidate]:
    distance = float(cfg.get("fixed_focus_distance_m", 2.0))
    tolerance = float(cfg.get("focus_tolerance_m", 0.35))
    # Candidate generation and the final optical judgement are deliberately
    # separate. With an uncalibrated centre-probe policy we only need a modest
    # continuous wall patch here; vision still approves the real surface.
    footprint_width = projection_footprint_geometry(cfg)["width_m"]
    calibration_mode = str(
        cfg.get("camera_projector_calibration", {}).get("mode", "")
    ).strip().lower()
    width_policy = str(cfg.get("candidate_wall_width_policy", "full_footprint")).strip().lower()
    if calibration_mode == "uncalibrated_center_probe" and width_policy == "center_probe":
        validation_width = min(
            footprint_width,
            max(0.30, float(cfg.get("candidate_probe_width_m", 0.60))),
        )
    else:
        validation_width = footprint_width
    horizontal_margin = max(0.0, float(cfg.get("projection_horizontal_margin_m", 0.15)))
    required_wall_length = max(
        float(cfg.get("minimum_wall_length_m", 1.5)),
        validation_width + 2.0 * horizontal_margin,
    )
    robot_clearance = float(cfg.get("robot_radius_m", 0.22)) + float(cfg.get("planning_margin_m", 0.08))
    minimum_path_clearance = max(
        robot_clearance,
        float(cfg.get("minimum_path_clearance_m", robot_clearance)),
    )
    minimum_goal_clearance = max(
        robot_clearance,
        float(cfg.get("minimum_goal_clearance_m", robot_clearance)),
    )
    max_normal_error = float(cfg.get("maximum_wall_normal_error_deg", 3.0))
    maximum = int(cfg.get("maximum_candidates", 12))
    corridor_margin = max(0.0, float(cfg.get("projection_corridor_margin_m", 0.10)))
    corridor_samples = max(3, int(cfg.get("projection_corridor_samples", 9)))
    candidates_per_side = max(1, int(cfg.get("candidates_per_wall_side", 4)))
    candidate_spacing = max(grid.resolution * 2.0, float(cfg.get("candidate_spacing_m", 0.35)))
    prefer_wall_center = bool(cfg.get("prefer_wall_center", True))
    center_fallback = max(0.0, float(cfg.get("wall_center_fallback_max_shift_m", 0.35)))
    center_probe_step = max(grid.resolution, float(cfg.get("wall_center_probe_step_m", 0.10)))
    clearance = grid.clearance_m()
    detection_cfg = dict(cfg)
    detection_cfg["minimum_wall_length_m"] = required_wall_length
    walls = [
        wall for wall in _group_wall_segments(_detect_wall_segments(grid, detection_cfg), detection_cfg)
        if float(wall["length"]) >= required_wall_length
    ]
    candidates: list[WallCandidate] = []
    for wall in walls:
        tangent = wall["tangent"]
        normal = wall["normal"]
        for sign in (1.0, -1.0):
            options = []
            usable_half = wall["length"] * 0.5 - validation_width * 0.5 - corridor_margin
            if usable_half < -grid.resolution:
                continue
            usable_half = max(0.0, usable_half)
            if prefer_wall_center:
                probe_half = min(usable_half, center_fallback)
                positive = list(np.arange(center_probe_step, probe_half + center_probe_step * 0.5, center_probe_step))
                if probe_half > 0.0 and (not positive or positive[-1] < probe_half - grid.resolution * 0.25):
                    positive.append(probe_half)
                raw_shifts = np.asarray([0.0] + positive + [-value for value in positive], dtype=float)
            else:
                sample_count = max(1, int(math.floor((usable_half * 2.0) / candidate_spacing)) + 1)
                raw_shifts = np.linspace(-usable_half, usable_half, sample_count) if sample_count > 1 else np.array([0.0])
            shifts = sorted(
                {round(float(value), 6) for value in raw_shifts} | {0.0},
                key=lambda value: (abs(value), value),
            )
            for signed in shifts:
                target = wall["center"] + tangent * signed
                nominal = target + normal * sign * distance
                cell = grid.world_to_cell(float(nominal[0]), float(nominal[1]))
                snapped = snap_free(grid, cell, robot_clearance, max_radius=5)
                if snapped is None:
                    continue
                x, y = grid.cell_to_world(snapped)
                position = np.array([x, y], dtype=float)
                view = target - position
                actual = float(np.linalg.norm(view))
                if abs(actual - distance) > min(tolerance, 0.10):
                    continue
                view_unit = view / max(actual, 1e-9)
                normal_error = math.degrees(math.asin(min(1.0, abs(float(np.dot(view_unit, tangent))))))
                if normal_error > max_normal_error:
                    continue
                if not wall_surface_supported(grid, target, tangent, validation_width, cfg):
                    continue
                target_cell = grid.world_to_cell(float(target[0]), float(target[1]))
                if not ray_clear_to_wall(grid, snapped, target_cell):
                    continue
                if not projection_corridor_clear(
                    grid, snapped, target, tangent, validation_width,
                    margin_m=corridor_margin, samples=corridor_samples,
                ):
                    continue
                if prefer_wall_center:
                    rank = (abs(float(signed)), normal_error, -float(clearance[snapped]))
                else:
                    rank = (-float(clearance[snapped]), normal_error, abs(float(signed)))
                options.append((*rank, snapped, position, target, actual))
            if not options:
                continue
            selected = []
            for option in sorted(options, key=lambda item: (item[0], item[1], item[2])):
                _, normal_error, _, cell, position, target, actual = option
                if float(clearance[cell]) + grid.resolution * 0.25 < minimum_goal_clearance:
                    continue
                if any(float(np.linalg.norm(target - old_target)) < candidate_spacing * 0.75 for old_target in selected):
                    continue
                yaw = math.atan2(float(target[1] - position[1]), float(target[0] - position[0]))
                candidates.append(WallCandidate(
                    id="", wall_id=wall["id"], x=float(position[0]), y=float(position[1]), yaw=yaw,
                    target_x=float(target[0]), target_y=float(target[1]), wall_length_m=float(wall["length"]),
                    wall_distance_m=actual, clearance_m=float(clearance[cell]), normal_error_deg=float(normal_error),
                    center_offset_m=float(np.linalg.norm(target - wall["center"])),
                ))
                selected.append(target)
                if len(selected) >= candidates_per_side:
                    break
    # Keep spatial alternatives internally so reachability can choose the best
    # side/offset, but expose only one final viewpoint per physical wall.
    main: list[WallCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (-item.clearance_m, item.normal_error_deg)):
        if any(math.hypot(candidate.x - old.x, candidate.y - old.y) < candidate_spacing * 0.65 for old in main):
            continue
        main.append(candidate)
    current = grid.world_to_cell(*start_xy)
    ordered: list[WallCandidate] = []
    remaining = main
    while remaining and len(ordered) < maximum:
        reachable = []
        for candidate in remaining:
            path = astar(grid, current, grid.world_to_cell(candidate.x, candidate.y), robot_clearance)
            if path:
                path_min_clearance = min(float(clearance[cell]) for cell in path)
                # The offline A* radius only proves geometric reachability.  A
                # real Nav2 controller also sees the inflated live costmap, so
                # reject tight routes before they can become hardware goals.
                if path_min_clearance + grid.resolution * 0.25 < minimum_path_clearance:
                    continue
                order = str(cfg.get("candidate_order", "shortest_path_then_goal_clearance_then_path_clearance"))
                if order == "goal_clearance_then_path_clearance_then_shortest_path":
                    rank = (-candidate.clearance_m, -path_min_clearance, len(path), -candidate.wall_length_m)
                else:
                    rank = (len(path), -candidate.clearance_m, -path_min_clearance, -candidate.wall_length_m)
                reachable.append((*rank, candidate, path, path_min_clearance))
        if not reachable:
            break
        _, _, _, _, chosen, path, path_min_clearance = min(reachable, key=lambda item: item[:4])
        chosen.path_cells = path
        chosen.path_length_m = max(0, len(path) - 1) * grid.resolution
        chosen.path_min_clearance_m = path_min_clearance
        ordered.append(chosen)
        current = path[-1]
        # Once one reachable pose has been selected for this physical wall,
        # discard its other internal alternatives. This keeps the public plan
        # readable and prevents repeated visits to the same wall.
        remaining = [item for item in remaining if item.wall_id != chosen.wall_id]
    for index, candidate in enumerate(ordered, 1):
        candidate.id = f"candidate_{index:02d}"
    return ordered


def visibility_mask(grid: GridMap, viewpoint: tuple[int, int], max_range_m: float) -> np.ndarray:
    visible = np.zeros_like(grid.data, dtype=np.uint8)
    if not grid.valid(viewpoint):
        return visible.astype(bool)
    radius = max(1, int(math.ceil(float(max_range_m) / grid.resolution)))
    row0, col0 = viewpoint
    # Direct DDA ray casting avoids allocating one temporary image per ray.
    # Ray count follows circumference so distant cells do not develop gaps.
    ray_count = max(240, min(720, int(math.ceil(2.0 * math.pi * radius * 0.80))))
    for angle in np.linspace(-math.pi, math.pi, ray_count, endpoint=False):
        sin_a, cos_a = math.sin(angle), math.cos(angle)
        previous = None
        for step in range(1, radius + 1):
            cell = int(round(row0 + sin_a * step)), int(round(col0 + cos_a * step))
            if cell == previous:
                continue
            previous = cell
            if not grid.valid(cell):
                break
            value = int(grid.data[cell])
            if value < 0 or value >= 65:
                break
            visible[cell] = 1
    visible[viewpoint] = 1
    return visible.astype(bool)


def choose_search_viewpoint(
    grid: GridMap,
    current_cell: tuple[int, int],
    covered: np.ndarray,
    cfg: dict[str, Any],
) -> tuple[SearchViewpoint | None, dict[str, Any]]:
    clearance_m = float(cfg.get("robot_radius_m", 0.22)) + float(cfg.get("planning_margin_m", 0.08))
    max_range_m = float(cfg.get("visual_scan_range_m", 4.0))
    reachable = reachable_mask(grid, current_cell, clearance_m)
    target = reachable & grid.free_mask()
    total = int(np.count_nonzero(target))
    already = int(np.count_nonzero(target & covered))
    uncovered = target & ~covered
    remaining = int(np.count_nonzero(uncovered))
    coverage = float(already / total) if total else 1.0
    diagnostics = {"reachable_cells": total, "covered_cells": already, "remaining_cells": remaining, "coverage": coverage}
    if not remaining:
        return None, diagnostics
    stride = max(1, int(round(float(cfg.get("viewpoint_spacing_m", 0.35)) / grid.resolution)))
    minimum_gain = max(1, int(round(float(cfg.get("minimum_gain_area_m2", 0.20)) / (grid.resolution ** 2))))
    best = None
    rows, cols = np.where(
        uncovered
        & ((np.indices(uncovered.shape)[0] % stride) == 0)
        & ((np.indices(uncovered.shape)[1] % stride) == 0)
    )
    candidates = list(zip(rows, cols))
    # Add cluster centroids so a thin occluded pocket is not skipped by stride.
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(uncovered.astype(np.uint8), 8)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] >= minimum_gain:
            candidates.append((int(round(centroids[index][1])), int(round(centroids[index][0]))))
    # Bound planning time on large maps.  Uniformly retain candidates across
    # the full list; connected-component centroids are appended below.
    maximum_candidates = max(12, int(cfg.get("maximum_viewpoint_candidates", 24)))
    if len(candidates) > maximum_candidates:
        indices = np.linspace(0, len(candidates) - 1, maximum_candidates, dtype=int)
        candidates = [candidates[int(index)] for index in indices]
    seen = set()
    for raw in candidates:
        cell = snap_free(grid, (int(raw[0]), int(raw[1])), clearance_m, max_radius=max(3, stride))
        if cell is None or cell in seen or not reachable[cell]:
            continue
        seen.add(cell)
        path = astar(grid, current_cell, cell, clearance_m)
        if not path:
            continue
        visible = visibility_mask(grid, cell, max_range_m)
        gain = int(np.count_nonzero(visible & uncovered))
        if gain < minimum_gain:
            continue
        path_length = max(0, len(path) - 1) * grid.resolution
        score = gain / (1.0 + path_length / max(grid.resolution, 0.05))
        if best is None or score > best[0]:
            after = float((already + gain) / total) if total else 1.0
            x, y = grid.cell_to_world(cell)
            best = (score, SearchViewpoint(x, y, 0.0, gain, path_length, min(1.0, after), path))
    return (best[1] if best else None), diagnostics


def _best_rectangular_split(
    cells: np.ndarray,
    minimum_cells: int,
    preferred_axis: int | None = None,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Split at a balanced low-density cross-section, usually a doorway/wall."""

    best = None
    axes = (int(preferred_axis),) if preferred_axis in (0, 1) else (0, 1)
    for axis in axes:
        values = cells[:, axis]
        low, high = int(values.min()), int(values.max())
        span = high - low + 1
        if span < 6:
            continue
        histogram = np.bincount(values - low, minlength=span)
        peak = max(1, int(histogram.max()))
        start = low + max(2, int(round(span * 0.20)))
        stop = high - max(2, int(round(span * 0.20)))
        for cut in range(start, stop + 1):
            selector = values <= cut
            left_count = int(np.count_nonzero(selector))
            right_count = int(len(cells) - left_count)
            if left_count < minimum_cells or right_count < minimum_cells:
                continue
            cross_density = float(histogram[cut - low]) / peak
            imbalance = abs(left_count - right_count) / len(cells)
            score = cross_density + 0.42 * imbalance
            if best is None or score < best[0]:
                best = (score, selector)
    if best is None:
        return None
    selector = best[1]
    return cells[selector], cells[~selector]


def rectangular_search_regions(
    grid: GridMap,
    start_cell: tuple[int, int],
    cfg: dict[str, Any],
) -> list[RectangularSearchRegion]:
    """Partition reachable free space into a small number of rectangles.

    The partition follows low-density cross-sections where possible, so doors
    and internal walls tend to become region boundaries.  Each rectangle uses
    the safe free cell nearest its geometric centre as the navigation target.
    """

    clearance_m = float(cfg.get("robot_radius_m", 0.22)) + float(cfg.get("planning_margin_m", 0.08))
    safe = reachable_mask(grid, start_cell, clearance_m) & grid.free_mask()
    rows, cols = np.where(safe)
    if not len(rows):
        return []
    all_cells = np.column_stack((rows, cols)).astype(np.int32)
    total_area = len(all_cells) * grid.resolution ** 2
    target_area = max(1.0, float(cfg.get("target_region_area_m2", 12.0)))
    maximum = max(1, int(cfg.get("maximum_regions", 5)))
    minimum = max(1, int(cfg.get("minimum_regions", 2))) if total_area >= target_area * 0.65 else 1
    desired = max(minimum, int(math.ceil(total_area / target_area)))
    desired = min(maximum, desired)
    minimum_cells = max(12, int(round(float(cfg.get("minimum_region_area_m2", 2.0)) / grid.resolution ** 2)))
    clusters = [all_cells]
    while len(clusters) < desired:
        candidates = sorted(range(len(clusters)), key=lambda index: len(clusters[index]), reverse=True)
        split_done = False
        for index in candidates:
            split = _best_rectangular_split(clusters[index], minimum_cells)
            if split is None:
                continue
            clusters[index:index + 1] = [split[0], split[1]]
            split_done = True
            break
        if not split_done:
            break

    # Large open rooms have no doorway bottleneck, so area-only partitioning
    # can still leave a dog several metres from the sole centre.  Continue
    # splitting along the best balanced cross-section until every rectangle's
    # farthest corner is inside the configured reliable visual distance.
    maximum_center_distance = max(0.5, float(cfg.get("maximum_region_center_distance_m", 2.4)))
    maximum_side = max(1.0, float(cfg.get("maximum_region_side_m", 4.2)))
    while len(clusters) < maximum:
        oversized = []
        for index, cells in enumerate(clusters):
            min_row, min_col = cells.min(axis=0)
            max_row, max_col = cells.max(axis=0)
            width_m = (int(max_col) - int(min_col) + 1) * grid.resolution
            height_m = (int(max_row) - int(min_row) + 1) * grid.resolution
            radius_m = 0.5 * math.hypot(width_m, height_m)
            excess = max(radius_m / maximum_center_distance, width_m / maximum_side, height_m / maximum_side)
            if excess > 1.0 + 1e-9:
                oversized.append((excess, len(cells), index))
        if not oversized:
            break
        split_done = False
        for _, _, index in sorted(oversized, reverse=True):
            cells = clusters[index]
            min_row, min_col = cells.min(axis=0)
            max_row, max_col = cells.max(axis=0)
            height_m = (int(max_row) - int(min_row) + 1) * grid.resolution
            width_m = (int(max_col) - int(min_col) + 1) * grid.resolution
            preferred_axis = 0 if height_m >= width_m else 1
            split = _best_rectangular_split(cells, minimum_cells, preferred_axis=preferred_axis)
            if split is None:
                split = _best_rectangular_split(cells, minimum_cells, preferred_axis=1 - preferred_axis)
            if split is None:
                continue
            clusters[index:index + 1] = [split[0], split[1]]
            split_done = True
            break
        if not split_done:
            break

    clearance = grid.clearance_m()
    unordered: list[tuple[RectangularSearchRegion, tuple[int, int]]] = []
    for cells in clusters:
        min_row, min_col = cells.min(axis=0)
        max_row, max_col = cells.max(axis=0)
        center_row = (float(min_row) + float(max_row)) * 0.5
        center_col = (float(min_col) + float(max_col)) * 0.5
        distances = (cells[:, 0] - center_row) ** 2 + (cells[:, 1] - center_col) ** 2
        nearest_order = np.argsort(distances)
        center_cell = tuple(map(int, cells[int(nearest_order[0])]))
        geometric_x = grid.origin_x + (center_col + 0.5) * grid.resolution
        geometric_y = grid.origin_y + (center_row + 0.5) * grid.resolution
        x, y = grid.cell_to_world(center_cell)
        corners = (
            (grid.origin_x + int(min_col) * grid.resolution, grid.origin_y + int(min_row) * grid.resolution),
            (grid.origin_x + (int(max_col) + 1) * grid.resolution, grid.origin_y + int(min_row) * grid.resolution),
            (grid.origin_x + (int(max_col) + 1) * grid.resolution, grid.origin_y + (int(max_row) + 1) * grid.resolution),
            (grid.origin_x + int(min_col) * grid.resolution, grid.origin_y + (int(max_row) + 1) * grid.resolution),
        )
        farthest = max(math.hypot(x - corner_x, y - corner_y) for corner_x, corner_y in corners)
        bbox_cells = max(1, (int(max_row) - int(min_row) + 1) * (int(max_col) - int(min_col) + 1))
        region = RectangularSearchRegion(
            id="",
            min_x=grid.origin_x + int(min_col) * grid.resolution,
            min_y=grid.origin_y + int(min_row) * grid.resolution,
            max_x=grid.origin_x + (int(max_col) + 1) * grid.resolution,
            max_y=grid.origin_y + (int(max_row) + 1) * grid.resolution,
            geometric_center_x=geometric_x,
            geometric_center_y=geometric_y,
            x=x,
            y=y,
            center_offset_m=float(math.hypot(x - geometric_x, y - geometric_y)),
            maximum_corner_distance_m=float(farthest),
            within_detection_distance=bool(farthest <= maximum_center_distance + grid.resolution * math.sqrt(2.0)),
            area_m2=float(len(cells) * grid.resolution ** 2),
            fill_ratio=float(len(cells) / bbox_cells),
        )
        unordered.append((region, center_cell))

    ordered: list[RectangularSearchRegion] = []
    current = start_cell
    remaining = list(unordered)
    while remaining:
        reachable = []
        for region, center_cell in remaining:
            path = astar(grid, current, center_cell, clearance_m)
            if path:
                reachable.append((len(path), region, center_cell, path))
        if not reachable:
            break
        _, region, center_cell, path = min(reachable, key=lambda item: item[0])
        region.id = f"region_{len(ordered) + 1:02d}"
        region.path_cells = path
        region.path_length_m = max(0, len(path) - 1) * grid.resolution
        ordered.append(region)
        current = center_cell
        remaining = [(item, cell) for item, cell in remaining if item is not region]
    return ordered


def footprint_polygon(frame_shape: Iterable[int], cfg: dict[str, Any]) -> np.ndarray:
    height, width = list(frame_shape)[:2]
    calibration = cfg.get("camera_projector_calibration") or {}
    points = calibration.get("normalized_footprint_polygon") or [
        [0.16, 0.16], [0.84, 0.16], [0.88, 0.84], [0.12, 0.84],
    ]
    polygon = np.asarray([[float(x) * width, float(y) * height] for x, y in points], dtype=np.float32)
    if polygon.shape != (4, 2):
        raise ValueError("camera_projector_calibration_requires_four_points")
    return np.round(polygon).astype(np.int32)


def projection_footprint_geometry(cfg: dict[str, Any]) -> dict[str, float]:
    distance = float(cfg.get("fixed_focus_distance_m", 2.0))
    throw_ratio = float(cfg.get("projector_throw_ratio", 1.20))
    aspect = str(cfg.get("projector_aspect_ratio", "16:9")).split(":")
    aspect_value = float(aspect[0]) / float(aspect[1])
    width = distance / max(throw_ratio, 0.2)
    return {
        "distance_m": distance,
        "width_m": width,
        "height_m": width / aspect_value,
        "throw_ratio": throw_ratio,
        "aspect_ratio": aspect_value,
    }


def analyze_projection_frame(frame: np.ndarray, cfg: dict[str, Any], output_path: str | Path | None = None) -> dict[str, Any]:
    calibration = cfg.get("camera_projector_calibration") or {}
    roi_mode = str(calibration.get("mode") or "uncalibrated_center_probe")
    polygon = footprint_polygon(frame.shape, cfg)
    mask = np.zeros(frame.shape[:2], np.uint8)
    cv2.fillConvexPoly(mask, polygon, 255)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    inside = mask > 0
    if not np.any(inside):
        raise ValueError("empty_projection_footprint")
    saturation = hsv[:, :, 1][inside]
    brightness = hsv[:, :, 2][inside]
    white = (saturation <= int(cfg.get("maximum_white_saturation", 58))) & (brightness >= int(cfg.get("minimum_white_brightness", 125)))
    edges = cv2.Canny(gray, 70, 160)
    white_ratio = float(np.mean(white))
    edge_density = float(np.mean(edges[inside] > 0))
    brightness_std = float(np.std(brightness))
    median_brightness = float(np.median(brightness))
    dark_ratio = float(np.mean(brightness < int(cfg.get("dark_pixel_brightness", 90))))
    # Low saturation alone also describes grey cabinets and beige boards. A
    # useful white projection patch must be both neutral and bright.
    bright_neutral = (
        (saturation <= int(cfg.get("maximum_neutral_saturation", 38)))
        & (brightness >= int(cfg.get("minimum_neutral_brightness", 180)))
    )
    bright_neutral_ratio = float(np.mean(bright_neutral))
    score = float(np.clip(
        0.52 * bright_neutral_ratio
        + 0.22 * (1.0 - min(1.0, edge_density / 0.12))
        + 0.14 * (1.0 - min(1.0, brightness_std / 75.0))
        + 0.12 * (1.0 - min(1.0, dark_ratio / 0.20)),
        0.0, 1.0,
    ))
    classical_ok = bool(
        white_ratio >= float(cfg.get("minimum_white_ratio", 0.68))
        and bright_neutral_ratio >= float(cfg.get("minimum_bright_neutral_ratio", 0.55))
        and median_brightness >= float(cfg.get("minimum_median_brightness", 175.0))
        and dark_ratio <= float(cfg.get("maximum_dark_ratio", 0.12))
        and edge_density <= float(cfg.get("maximum_edge_density", 0.10))
        and brightness_std <= float(cfg.get("maximum_brightness_std", 68.0))
    )
    annotated = frame.copy()
    cv2.polylines(annotated, [polygon], True, (0, 255, 0) if classical_ok else (0, 0, 255), 3)
    roi_label = "center probe" if roi_mode == "uncalibrated_center_probe" else "projector footprint"
    label = (
        f"{roi_label} neutral={bright_neutral_ratio:.2f} "
        f"median={median_brightness:.0f} edge={edge_density:.3f} score={score:.2f}"
    )
    cv2.putText(annotated, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(annotated, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), annotated, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return {
        "ok": classical_ok,
        "score": score,
        "white_ratio": white_ratio,
        "bright_neutral_ratio": bright_neutral_ratio,
        "median_brightness": median_brightness,
        "dark_ratio": dark_ratio,
        "edge_density": edge_density,
        "brightness_std": brightness_std,
        "footprint_polygon_px": polygon.tolist(),
        "footprint_geometry": projection_footprint_geometry(cfg),
        "roi_mode": roi_mode,
        "calibrated": bool(calibration.get("calibrated", False)),
        "provider": "local_projection_roi",
    }


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        left, right = cleaned.find("{"), cleaned.rfind("}")
        if left < 0 or right <= left:
            raise ValueError("vlm_response_without_json")
        return json.loads(cleaned[left:right + 1])


def _load_simple_env(path: str | Path) -> dict[str, str]:
    """Read the small, root-owned provider env file without logging secrets."""
    values: dict[str, str] = {}
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in {"PUCODING_API_KEY", "PUCODING_BASE_URL", "PUCODING_MODEL"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def modelscope_projection_judge(image_path: str | Path, local: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("MODELSCOPE_SDK_TOKEN") or os.getenv("MODELSCOPE_API_KEY")
    if not token:
        raise RuntimeError("modelscope_token_missing")
    path = Path(image_path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    geometry = local.get("footprint_geometry") or {}
    roi_mode = str(local.get("roi_mode") or "")
    if roi_mode == "uncalibrated_center_probe":
        region_instruction = (
            "当前相机与光机尚未做四角标定。图中绿色四边形是用户指定的摄像头中心小块采样区，"
            "不是完整投影范围。只判断绿色中心区域；只要该中心区域适合投影就判定为通过，"
            "绿色区域之外的物体、纹理或边界不要作为否决依据。完整墙宽已由激光地图单独检查。"
        )
    else:
        region_instruction = (
            "图中绿色四边形不是整个摄像头视野，而是经过相机与光机四角标定得到的实际投影覆盖区域，"
            "必须检查整个绿色区域。"
        )
    prompt = (
        "你是投影表面检查器。" + region_instruction + "机器人与墙的目标距离约为"
        f"{geometry.get('distance_m', 2.0):.2f}米，实际投影宽约{geometry.get('width_m', 1.67):.2f}米、"
        f"高约{geometry.get('height_m', 0.94):.2f}米。绿色区域必须是足够平整、浅色、连续的墙或幕布，"
        "绿色区域内不得包含门窗、电视、柜子、人物、强反光或明显纹理。严格返回JSON："
        '{"ok":true或false,"confidence":0到1,"surface":"wall|screen|other",'
        '"blockers":[],"reason":"简短中文理由"}。'
    )
    payload = {
        "model": str(cfg.get("modelscope_model", "Qwen/Qwen3-VL-8B-Instruct")),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}},
        ]}],
        "temperature": 0.05,
        "max_tokens": 240,
        "stream": False,
    }
    request = urllib.request.Request(
        str(cfg.get("modelscope_base_url", "https://api-inference.modelscope.cn/v1/chat/completions")),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.perf_counter()
    last_error = None
    for attempt in range(3):
        try:
            with opener.open(request, timeout=float(cfg.get("modelscope_timeout_sec", 20.0))) as response:
                raw = json.loads(response.read().decode("utf-8", "replace"))
            answer = _extract_json(raw["choices"][0]["message"]["content"])
            confidence = float(answer.get("confidence") or 0.0)
            answer["ok"] = bool(answer.get("ok")) and confidence >= float(cfg.get("minimum_vlm_confidence", 0.75))
            answer["confidence"] = confidence
            answer["provider"] = "modelscope"
            answer["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            return answer
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"modelscope_judge_failed:{type(last_error).__name__}:{last_error}")


def pucoding_projection_judge(image_path: str | Path, local: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Strictly validate a projection region with PuCoding's OpenAI-compatible vision API.

    The classical detector remains a cheap pre-filter.  This call is the final
    semantic check.  Cosmetic defects are acceptable, while real obstructions
    and unsafe/non-projectable surfaces must still be rejected.
    """
    provider_env = _load_simple_env(
        str(cfg.get("pucoding_env_file") or "/home/test/.config/white_wall_pucoding.env")
    )
    token = (
        os.getenv("PUCODING_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or provider_env.get("PUCODING_API_KEY")
    )
    base_url = os.getenv("PUCODING_BASE_URL") or provider_env.get("PUCODING_BASE_URL") or str(
        cfg.get("pucoding_base_url", "https://pucoding.com/v1/chat/completions")
    )
    fallback_url = str(cfg.get("pucoding_base_url", "https://pucoding.com/v1/chat/completions"))
    endpoints = [base_url]
    if fallback_url and fallback_url not in endpoints:
        endpoints.append(fallback_url)
    if not token:
        raise RuntimeError("pucoding_token_missing")
    path = Path(image_path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    geometry = local.get("footprint_geometry") or {}
    prompt = (
        "你是机器人投影表面的严格安全检查器。图中的绿色矩形是必须完整检查的拟投影区域。"
        "绿色矩形内约75%以上是连续、基本平整、浅色墙面或幕布即可通过，"
        "不要求表面完美。少量污渍、轻微划痕、轻微色差、小接缝，以及少量墙壁开关、"
        "电源插座、网口或小型固定面板都不影响正常投影，不能把它们当作否决项。"
        "只有当这些小设施合计占用投影区域明显过大时才拒绝。人物、家具、纸箱、显示器、"
        "门窗、大型墙面设施、强反光、明显凹凸或大面积遮挡"
        "仍必须拒绝；不能因为只有一小块是白色就通过。"
        "如果画面角度或信息不足以确定完整区域适合投影，也必须拒绝。"
        f"规划距离约{float(geometry.get('distance_m', 2.0)):.2f}米。"
        "只输出严格JSON，不要输出Markdown："
        '{"ok":false,"confidence":0.0,"usable_ratio":0.0,'
        '"surface":"wall|screen|other","blockers":["..."],"reason":"简短中文理由"}'
    )
    payload = {
        "model": str(
            os.getenv("PUCODING_MODEL")
            or provider_env.get("PUCODING_MODEL")
            or cfg.get("pucoding_model", "gpt-5.6-luna")
        ),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}},
        ]}],
        "temperature": 0,
        "max_tokens": int(cfg.get("pucoding_max_tokens", 220)),
        "stream": False,
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.perf_counter()
    attempts = max(1, int(cfg.get("pucoding_attempts", 2)))
    last_error = None
    for endpoint in endpoints:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(attempts):
            try:
                with opener.open(request, timeout=float(cfg.get("pucoding_timeout_sec", 35.0))) as response:
                    raw = json.loads(response.read().decode("utf-8", "replace"))
                answer = _extract_json(raw["choices"][0]["message"]["content"])
                confidence = float(answer.get("confidence") or 0.0)
                blockers = answer.get("blockers")
                if not isinstance(blockers, list):
                    raise ValueError("pucoding_blockers_not_list")
                answer["ok"] = bool(answer.get("ok")) and confidence >= float(
                    cfg.get("minimum_vlm_confidence", 0.75)
                )
                answer["confidence"] = confidence
                answer["provider"] = "pucoding"
                answer["model"] = payload["model"]
                answer["transport"] = "loopback_relay" if "127.0.0.1" in endpoint else "direct_https"
                answer["usage"] = raw.get("usage")
                answer["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
                return answer
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"pucoding_judge_failed:{type(last_error).__name__}:{last_error}")


def save_exploration_overview(
    grid: GridMap,
    path: str | Path,
    candidates: list[WallCandidate] | None = None,
    viewpoints: list[SearchViewpoint] | None = None,
    regions: list[RectangularSearchRegion] | None = None,
    covered: np.ndarray | None = None,
) -> None:
    image = np.full((*grid.data.shape, 3), (205, 205, 205), np.uint8)
    image[grid.free_mask()] = (255, 255, 255)
    image[grid.occupied_mask()] = (0, 0, 0)
    if covered is not None:
        image[covered & grid.free_mask()] = (210, 245, 210)
    for candidate in candidates or []:
        cell = grid.world_to_cell(candidate.x, candidate.y)
        target = grid.world_to_cell(candidate.target_x, candidate.target_y)
        if grid.valid(cell):
            cv2.circle(image, (cell[1], cell[0]), 4, (0, 0, 255), -1)
            if grid.valid(target):
                cv2.line(image, (cell[1], cell[0]), (target[1], target[0]), (255, 0, 0), 1)
    for viewpoint in viewpoints or []:
        cell = grid.world_to_cell(viewpoint.x, viewpoint.y)
        if grid.valid(cell):
            cv2.circle(image, (cell[1], cell[0]), 4, (255, 0, 255), -1)
    for index, region in enumerate(regions or [], 1):
        lower = grid.world_to_cell(region.min_x, region.min_y)
        upper = grid.world_to_cell(region.max_x - grid.resolution * 0.5, region.max_y - grid.resolution * 0.5)
        center = grid.world_to_cell(region.x, region.y)
        color = ((37 * index) % 220 + 25, (91 * index) % 190 + 35, (53 * index) % 210 + 30)
        cv2.rectangle(image, (lower[1], lower[0]), (upper[1], upper[0]), color, 1)
        if grid.valid(center):
            cv2.circle(image, (center[1], center[0]), 4, color, -1)
    output = np.flipud(image)
    output = cv2.resize(output, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_NEAREST)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(destination), output)
