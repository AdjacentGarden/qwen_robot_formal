#!/usr/bin/env python3

import json
import math

import numpy as np

from exploration_core import (
    _detect_wall_segments,
    _group_wall_segments,
    astar,
    load_map_yaml,
    projection_corridor_clear,
    projection_footprint_geometry,
    ray_clear_to_wall,
    wall_surface_supported,
)

cfg = json.load(open("config/exploration.json", encoding="utf-8"))["projection"]
grid = load_map_yaml("/home/test/project_0727_fixed_points_home_scenes/maps/navigation/current.yaml")
width = projection_footprint_geometry(cfg)["width_m"]
required = max(cfg["minimum_wall_length_m"], width + 2 * cfg["projection_horizontal_margin_m"])
walls = _group_wall_segments(_detect_wall_segments(grid, {**cfg, "minimum_wall_length_m": required}), cfg)
wall = next(item for item in walls if item["id"] == "wall_03")
clearance = grid.clearance_m()
robot_clearance = cfg["robot_radius_m"] + cfg["planning_margin_m"]
origin = grid.world_to_cell(0.0, 0.0)
found = []
for signed in np.linspace(-0.45, 0.45, 19):
    target = wall["center"] + wall["tangent"] * signed
    nominal = target + wall["normal"] * 2.0
    nr, nc = grid.world_to_cell(*nominal)
    for row in range(nr - 16, nr + 17):
        for col in range(nc - 16, nc + 17):
            cell = (row, col)
            if not grid.valid(cell) or not grid.free_mask()[cell] or clearance[cell] < robot_clearance:
                continue
            position = np.asarray(grid.cell_to_world(cell), dtype=float)
            view = target - position
            distance = float(np.linalg.norm(view))
            if abs(distance - 2.0) > 0.10:
                continue
            unit = view / distance
            error = math.degrees(math.asin(min(1.0, abs(float(np.dot(unit, wall["tangent"]))))))
            if error > cfg["maximum_wall_normal_error_deg"]:
                continue
            if not wall_surface_supported(grid, target, wall["tangent"], width, cfg):
                continue
            if not ray_clear_to_wall(grid, cell, grid.world_to_cell(*target)):
                continue
            if not projection_corridor_clear(grid, cell, target, wall["tangent"], width, cfg["projection_corridor_margin_m"], cfg["projection_corridor_samples"]):
                continue
            path = astar(grid, origin, cell, robot_clearance)
            if not path:
                continue
            found.append((float(np.linalg.norm(position - nominal)), -float(clearance[cell]), position, target, distance, error, len(path)))
found.sort(key=lambda item: (item[0], item[1], item[5]))
print("feasible_count", len(found))
for item in found[:10]:
    print({
        "nominal_shift_m": round(item[0], 3),
        "clearance_m": round(-item[1], 3),
        "position": [round(float(value), 3) for value in item[2]],
        "target": [round(float(value), 3) for value in item[3]],
        "distance_m": round(item[4], 3),
        "normal_error_deg": round(item[5], 3),
        "path_cells": item[6],
    })
