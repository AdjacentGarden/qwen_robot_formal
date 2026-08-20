#!/usr/bin/env python3

import json

from exploration_core import load_map_yaml, projection_candidates, rectangular_search_regions

cfg = json.load(open("config/exploration.json", encoding="utf-8"))
grid = load_map_yaml("/home/test/project_0727_fixed_points_home_scenes/maps/navigation/current.yaml")
start = grid.world_to_cell(0.0, 0.0)
clearance = grid.clearance_m()
for threshold in (0.35, 0.40, 0.45, 0.50):
    projection_cfg = dict(cfg["projection"])
    projection_cfg["minimum_path_clearance_m"] = threshold
    projection = projection_candidates(grid, (0.0, 0.0), projection_cfg)
    safe_candidates = [
        (item.id, round(item.x, 3), round(item.y, 3), round(item.path_min_clearance_m, 3))
        for item in projection
    ]
    pet_cfg = dict(cfg["pet_search"])
    pet_cfg["planning_margin_m"] = threshold - float(pet_cfg["robot_radius_m"])
    regions = rectangular_search_regions(grid, start, pet_cfg)
    print(json.dumps({
        "threshold_m": threshold,
        "projection_candidates": safe_candidates,
        "pet_region_count": len(regions),
        "pet_all_within_range": bool(regions) and all(item.within_detection_distance for item in regions),
        "pet_path_min_clearances": [
            round(min(float(clearance[cell]) for cell in item.path_cells), 3) for item in regions
        ],
    }, ensure_ascii=False))
