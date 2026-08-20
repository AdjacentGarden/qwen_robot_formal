#!/usr/bin/env python3

import json
import math

from exploration_core import load_map_yaml, projection_candidates

cfg = json.load(open("config/exploration.json", encoding="utf-8"))["projection"]
grid = load_map_yaml("/home/test/project_0727_fixed_points_home_scenes/maps/navigation/current.yaml")
clearance = grid.clearance_m()
candidates = projection_candidates(grid, (0.0, 0.0), cfg)
rows = []
for item in candidates:
    values = [float(clearance[cell]) for cell in item.path_cells]
    rows.append({
        "id": item.id,
        "goal": [round(item.x, 3), round(item.y, 3)],
        "goal_clearance_m": round(item.clearance_m, 3),
        "path_minimum_clearance_m": round(min(values), 3),
        "path_cells_below_nav2_inflation_radius": sum(value < 0.45 for value in values),
        "path_cells": len(values),
    })
print(json.dumps({
    "exploration_required_clearance_m": cfg["robot_radius_m"] + cfg["planning_margin_m"],
    "nav2_footprint_corner_radius_with_padding_m": round(math.hypot(0.2, 0.2) + 0.02, 3),
    "nav2_inflation_radius_m": 0.45,
    "candidates": rows,
}, ensure_ascii=False, indent=2))
