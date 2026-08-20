#!/usr/bin/env python3
"""Explain every projection-candidate rejection on the current offline map."""

import json
import math

import numpy as np

from exploration_core import (
    _detect_wall_segments,
    _group_wall_segments,
    astar,
    load_map_yaml,
    projection_candidates,
    projection_corridor_clear,
    projection_footprint_geometry,
    ray_clear_to_wall,
    snap_free,
    wall_surface_supported,
)


def main() -> None:
    cfg = json.load(open("config/exploration.json", encoding="utf-8"))["projection"]
    grid = load_map_yaml("/home/test/project_0727_fixed_points_home_scenes/maps/navigation/current.yaml")
    distance = float(cfg["fixed_focus_distance_m"])
    tolerance = float(cfg["focus_tolerance_m"])
    footprint_width = projection_footprint_geometry(cfg)["width_m"]
    horizontal_margin = float(cfg["projection_horizontal_margin_m"])
    required_length = max(float(cfg["minimum_wall_length_m"]), footprint_width + 2 * horizontal_margin)
    robot_clearance = float(cfg["robot_radius_m"]) + float(cfg["planning_margin_m"])
    max_normal_error = float(cfg["maximum_wall_normal_error_deg"])
    corridor_margin = float(cfg["projection_corridor_margin_m"])
    corridor_samples = int(cfg["projection_corridor_samples"])
    detected = _detect_wall_segments(grid, {**cfg, "minimum_wall_length_m": required_length})
    walls = [wall for wall in _group_wall_segments(detected, cfg) if wall["length"] >= required_length]
    final = {item.wall_id: item for item in projection_candidates(grid, (0.0, 0.0), cfg)}
    output = []
    for wall in walls:
        tangent = wall["tangent"]
        normal = wall["normal"]
        report = {
            "wall_id": wall["id"],
            "center": [round(float(value), 3) for value in wall["center"]],
            "length_m": round(float(wall["length"]), 3),
            "angle_deg": round(math.degrees(math.atan2(float(tangent[1]), float(tangent[0]))), 3),
            "final_candidate": wall["id"] in final,
            "signs": [],
        }
        for sign in (1.0, -1.0):
            failures = {name: 0 for name in (
                "wall_extent", "no_safe_cell", "focus_distance", "normal_error",
                "wall_surface_gap", "center_ray_blocked", "projection_fan_blocked", "passed_geometry",
            )}
            passing = []
            examples = {}
            for shift in np.linspace(0.0, min(0.45, wall["length"] * 0.22), 10):
                for signed in ((0.0,) if shift == 0 else (shift, -shift)):
                    if abs(float(signed)) + footprint_width * 0.5 + corridor_margin > wall["length"] * 0.5:
                        failures["wall_extent"] += 1
                        continue
                    target = wall["center"] + tangent * signed
                    nominal = target + normal * sign * distance
                    cell = grid.world_to_cell(float(nominal[0]), float(nominal[1]))
                    snapped = snap_free(grid, cell, robot_clearance, max_radius=5)
                    if snapped is None:
                        failures["no_safe_cell"] += 1
                        examples.setdefault("no_safe_cell", {
                            "nominal": [round(float(value), 3) for value in nominal],
                            "map_cell": list(cell),
                        })
                        continue
                    position = np.asarray(grid.cell_to_world(snapped), dtype=float)
                    view = target - position
                    actual = float(np.linalg.norm(view))
                    if abs(actual - distance) > min(tolerance, 0.10):
                        failures["focus_distance"] += 1
                        examples.setdefault("focus_distance", {
                            "nominal": [round(float(value), 3) for value in nominal],
                            "snapped_position": [round(float(value), 3) for value in position],
                            "target": [round(float(value), 3) for value in target],
                            "actual_distance_m": round(actual, 3),
                            "distance_error_m": round(abs(actual - distance), 3),
                        })
                        continue
                    view_unit = view / max(actual, 1e-9)
                    normal_error = math.degrees(math.asin(min(1.0, abs(float(np.dot(view_unit, tangent))))))
                    if normal_error > max_normal_error:
                        failures["normal_error"] += 1
                        continue
                    if not wall_surface_supported(grid, target, tangent, footprint_width, cfg):
                        failures["wall_surface_gap"] += 1
                        continue
                    target_cell = grid.world_to_cell(float(target[0]), float(target[1]))
                    if not ray_clear_to_wall(grid, snapped, target_cell):
                        failures["center_ray_blocked"] += 1
                        continue
                    if not projection_corridor_clear(
                        grid, snapped, target, tangent, footprint_width,
                        margin_m=corridor_margin, samples=corridor_samples,
                    ):
                        failures["projection_fan_blocked"] += 1
                        continue
                    failures["passed_geometry"] += 1
                    path = astar(grid, grid.world_to_cell(0.0, 0.0), snapped, robot_clearance)
                    passing.append({
                        "position": [round(float(value), 3) for value in position],
                        "target": [round(float(value), 3) for value in target],
                        "path_from_origin": bool(path),
                    })
            report["signs"].append({"sign": sign, "counts": failures, "examples": examples, "passing": passing[:2]})
        output.append(report)
    print(json.dumps({"required_wall_length_m": required_length, "walls": output}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
