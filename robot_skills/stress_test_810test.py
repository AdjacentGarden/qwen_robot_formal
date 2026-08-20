#!/usr/bin/env python3
"""Offline robustness test for wall candidates on the 810test occupancy map."""

import json

import numpy as np

from exploration_core import GridMap, load_map_yaml, projection_candidates


def main() -> None:
    config = json.load(open("config/exploration.json", encoding="utf-8"))["projection"]
    grid = load_map_yaml("/home/test/project_0727_fixed_points_home_scenes/maps/navigation/current.yaml")
    baseline = projection_candidates(grid, (0.0, 0.0), config)
    baseline_targets = np.asarray([[item.target_x, item.target_y] for item in baseline], dtype=float)
    occupied = np.argwhere(grid.occupied_mask())
    report = {
        "hardware_used": False,
        "baseline_candidates": len(baseline),
        "baseline_targets": baseline_targets.round(3).tolist(),
        "stress": [],
    }
    for rate in (0.01, 0.03, 0.05):
        preserved = []
        counts = []
        for seed in range(20):
            rng = np.random.default_rng(810000 + seed + int(rate * 1000))
            data = grid.data.copy()
            chosen = occupied[rng.random(len(occupied)) < rate]
            if len(chosen):
                data[chosen[:, 0], chosen[:, 1]] = 0
            trial = GridMap(
                data=data,
                resolution=grid.resolution,
                origin_x=grid.origin_x,
                origin_y=grid.origin_y,
                frame_id=grid.frame_id,
                stamp_sec=0.0,
                source=f"stress_dropout_{rate}",
            )
            candidates = projection_candidates(trial, (0.0, 0.0), config)
            counts.append(len(candidates))
            targets = np.asarray([[item.target_x, item.target_y] for item in candidates], dtype=float)
            matched = 0
            if len(targets):
                matched = sum(
                    float(np.min(np.linalg.norm(targets - baseline_target, axis=1))) <= 0.45
                    for baseline_target in baseline_targets
                )
            preserved.append(int(matched))
        report["stress"].append({
            "occupied_cell_dropout_ratio": rate,
            "trials": 20,
            "all_baseline_walls_preserved": sum(value == len(baseline) for value in preserved),
            "minimum_baseline_walls_preserved": min(preserved),
            "candidate_count_min": min(counts),
            "candidate_count_max": max(counts),
        })
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
