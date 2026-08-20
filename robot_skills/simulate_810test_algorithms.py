#!/usr/bin/env python3
"""Hardware-free planning simulation for projection and rectangular pet search."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from exploration_core import (
    load_map_yaml,
    projection_candidates,
    rectangular_search_regions,
)


ROOT = Path(__file__).resolve().parent
MAP_YAML = Path("/home/test/project_0727_fixed_points_home_scenes/maps/navigation/current.yaml")
OUTPUT = ROOT / "simulation_results_810test"


def cells_to_world(grid, cells):
    output = []
    for index, cell in enumerate(cells or []):
        if index % 3 and index != len(cells) - 1:
            continue
        x, y = grid.cell_to_world((int(cell[0]), int(cell[1])))
        output.append([round(float(x), 4), round(float(y), 4)])
    return output


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / "config" / "exploration.json").read_text(encoding="utf-8"))
    grid = load_map_yaml(MAP_YAML)
    start = (0.0, 0.0)
    candidates = projection_candidates(grid, start, config["projection"])
    regions = rectangular_search_regions(
        grid,
        grid.world_to_cell(*start),
        config["pet_search"],
    )

    image = np.full((*grid.data.shape, 3), 205, np.uint8)
    image[grid.free_mask()] = (250, 250, 250)
    image[grid.occupied_mask()] = (20, 20, 20)
    image = np.flipud(image)
    cv2.imwrite(str(OUTPUT / "map_810test.png"), image)

    projection = []
    for candidate in candidates:
        item = candidate.public()
        item["path_world"] = cells_to_world(grid, candidate.path_cells)
        projection.append(item)

    pet_regions = []
    for index, region in enumerate(regions, 1):
        item = region.public()
        item["path_world"] = cells_to_world(grid, region.path_cells)
        item["scan_direction"] = "counterclockwise" if index % 2 else "clockwise"
        item["worst_case_dog_found"] = index == len(regions)
        pet_regions.append(item)

    report = {
        "ok": bool(candidates) and bool(regions) and all(
            item.within_detection_distance for item in regions
        ),
        "hardware_used": False,
        "map": grid.to_json_summary(),
        "start": {"x": start[0], "y": start[1]},
        "projection": {
            "candidate_count": len(projection),
            "candidates": projection,
            "geometry_passed": len(projection),
            "visual_judgement_executed": False,
            "note": "候选点只通过地图几何约束，实际白墙仍需摄像头和视觉模型复核。",
        },
        "pet_search": {
            "region_count": len(pet_regions),
            "regions": pet_regions,
            "all_regions_within_detection_distance": all(
                item.within_detection_distance for item in regions
            ),
            "worst_case_simulation": {
                "dog_region": pet_regions[-1]["id"] if pet_regions else None,
                "scan_count": len(pet_regions),
                "result": "found" if pet_regions else "incomplete",
            },
            "not_found_simulation": {
                "scan_count": len(pet_regions),
                "result": "not_found" if pet_regions else "incomplete",
                "exhausted": bool(pet_regions),
            },
        },
    }
    (OUTPUT / "simulation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
