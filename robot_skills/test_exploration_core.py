#!/usr/bin/env python3

import json
import unittest
from pathlib import Path

import cv2
import numpy as np

from exploration_core import (
    GridMap,
    _detect_wall_segments,
    _group_wall_segments,
    _segment_geometry,
    analyze_projection_frame,
    load_map_yaml,
    projection_corridor_clear,
    projection_candidates,
    rectangular_search_regions,
    wall_surface_supported,
)


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config" / "exploration.json").read_text(encoding="utf-8"))
REAL_MAP = Path("/home/test/project_0727_fixed_points_home_scenes/test_assets/maps/713_test.yaml")
CURRENT_MAP = Path("/home/test/project_0727_fixed_points_home_scenes/maps/navigation/current.yaml")


class ExplorationCoreTests(unittest.TestCase):
    def test_short_broken_wall_pieces_are_kept_until_after_grouping(self):
        data = np.zeros((140, 140), dtype=np.int16)
        # Two individually short wall pieces with a 0.55 m break.  The break is
        # larger than Hough's own line-gap tolerance but small enough to be map
        # noise rather than a separate physical wall.
        cv2.line(data, (55, 20), (55, 46), 100, 2)
        cv2.line(data, (55, 58), (55, 84), 100, 2)
        grid = GridMap(data=data, resolution=0.05, origin_x=0.0, origin_y=0.0)
        cfg = dict(CONFIG["projection"])
        cfg["minimum_wall_length_m"] = 1.97
        segments = _detect_wall_segments(grid, cfg)
        self.assertTrue(segments)
        self.assertTrue(any(item["length"] < 1.97 for item in segments))
        groups = _group_wall_segments(segments, cfg)
        self.assertTrue(any(item["length"] >= 1.97 for item in groups))

    def test_slightly_tilted_collinear_pieces_form_one_physical_wall(self):
        grid = GridMap(data=np.zeros((160, 160), dtype=np.int16), resolution=0.05, origin_x=0.0, origin_y=0.0)
        pieces = [
            _segment_geometry((20, 60, 60, 60), grid),
            _segment_geometry((64, 61, 104, 65), grid),
        ]
        groups = _group_wall_segments(pieces, CONFIG["projection"])
        self.assertEqual(len(groups), 1)
        self.assertGreater(groups[0]["length"], 4.0)
        self.assertLess(groups[0]["fit_max_m"], CONFIG["projection"]["wall_group_max_fit_residual_m"])

    def test_projector_footprint_does_not_bridge_a_doorway(self):
        data = np.zeros((160, 160), dtype=np.int16)
        # A vertical wall with a 0.70 m doorway centred on the intended image.
        cv2.line(data, (80, 20), (80, 62), 100, 2)
        cv2.line(data, (80, 77), (80, 120), 100, 2)
        grid = GridMap(data=data, resolution=0.05, origin_x=0.0, origin_y=0.0)
        target = np.asarray(grid.cell_to_world((69, 80)), dtype=float)
        tangent = np.asarray([0.0, 1.0], dtype=float)
        self.assertFalse(wall_surface_supported(grid, target, tangent, 1.67, CONFIG["projection"]))

    @unittest.skipUnless(REAL_MAP.exists(), "copied real map is unavailable")
    def test_one_safe_reachable_candidate_per_physical_wall(self):
        grid = load_map_yaml(REAL_MAP)
        candidates = projection_candidates(grid, (0.0, 0.0), CONFIG["projection"])
        self.assertGreaterEqual(len(candidates), 2)
        by_wall = {}
        for item in candidates:
            by_wall.setdefault(item.wall_id, []).append(item)
        self.assertEqual(len(candidates), len(by_wall))
        self.assertTrue(all(len(items) == 1 for items in by_wall.values()))
        self.assertTrue(all(
            item.center_offset_m <= CONFIG["projection"]["wall_center_fallback_max_shift_m"] + grid.resolution
            for item in candidates
        ))
        required_width = CONFIG["projection"]["candidate_probe_width_m"]
        minimum_wall = max(
            CONFIG["projection"]["minimum_wall_length_m"],
            required_width + 2.0 * CONFIG["projection"]["projection_horizontal_margin_m"],
        )
        for item in candidates:
            self.assertLessEqual(abs(item.wall_distance_m - 2.0), 0.10)
            self.assertLessEqual(item.normal_error_deg, CONFIG["projection"]["maximum_wall_normal_error_deg"])
            self.assertGreaterEqual(item.wall_length_m, minimum_wall)
            self.assertGreater(item.path_length_m, 0.0)
            self.assertGreaterEqual(
                item.path_min_clearance_m + grid.resolution * 0.25,
                CONFIG["projection"]["minimum_path_clearance_m"],
            )
            self.assertGreaterEqual(
                item.clearance_m + grid.resolution * 0.25,
                CONFIG["projection"]["minimum_goal_clearance_m"],
            )
            tangent = np.asarray([-np.sin(item.yaw), np.cos(item.yaw)], dtype=float)
            target = np.asarray([item.target_x, item.target_y], dtype=float)
            self.assertTrue(projection_corridor_clear(
                grid,
                grid.world_to_cell(item.x, item.y),
                target,
                tangent,
                required_width - 2.0 * CONFIG["projection"]["projection_horizontal_margin_m"],
                CONFIG["projection"]["projection_corridor_margin_m"],
                CONFIG["projection"]["projection_corridor_samples"],
            ))

    @unittest.skipUnless(CURRENT_MAP.exists(), "current robot map is unavailable")
    def test_current_map_prefers_wall_centres_at_two_metres(self):
        grid = load_map_yaml(CURRENT_MAP)
        candidates = projection_candidates(grid, (0.0, 0.0), CONFIG["projection"])
        # Cartographer's current exported occupancy image can gain or lose a
        # marginal wall fragment as the map is refreshed.  The invariant is one
        # safe candidate per accepted physical wall, not an exact global count.
        self.assertGreaterEqual(len(candidates), 4)
        self.assertLessEqual(len(candidates), CONFIG["projection"]["maximum_candidates"])
        self.assertEqual(len(candidates), len({item.wall_id for item in candidates}))
        self.assertGreaterEqual(sum(item.center_offset_m <= grid.resolution + 1e-6 for item in candidates), 3)
        for item in candidates:
            self.assertLessEqual(
                item.center_offset_m,
                CONFIG["projection"]["wall_center_fallback_max_shift_m"] + grid.resolution,
            )
            self.assertLessEqual(abs(item.wall_distance_m - 2.0), 0.10)

    @unittest.skipUnless(REAL_MAP.exists(), "copied real map is unavailable")
    def test_reachable_map_is_partitioned_into_few_rectangles(self):
        grid = load_map_yaml(REAL_MAP)
        current = grid.world_to_cell(0.0, 0.0)
        regions = rectangular_search_regions(grid, current, CONFIG["pet_search"])
        self.assertGreaterEqual(len(regions), 3)
        self.assertLessEqual(len(regions), CONFIG["pet_search"]["maximum_regions"])
        self.assertEqual([item.id for item in regions], [f"region_{index:02d}" for index in range(1, len(regions) + 1)])
        for region in regions:
            self.assertLess(region.min_x, region.max_x)
            self.assertLess(region.min_y, region.max_y)
            self.assertGreater(region.area_m2, 0.0)
            self.assertGreater(region.fill_ratio, 0.0)
            self.assertTrue(region.within_detection_distance, region.public())
            self.assertLessEqual(
                region.maximum_corner_distance_m,
                CONFIG["pet_search"]["maximum_region_center_distance_m"] + grid.resolution * np.sqrt(2.0),
            )
            self.assertGreaterEqual(region.x, region.min_x)
            self.assertLessEqual(region.x, region.max_x)
            self.assertGreaterEqual(region.y, region.min_y)
            self.assertLessEqual(region.y, region.max_y)
            self.assertGreaterEqual(region.path_length_m, 0.0)

    def test_real_captures_distinguish_white_wall_from_board_and_cabinet(self):
        root = Path("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test/robot_skills/runtime/exploration")
        samples = {
            "board": root / "projection_1786301148" / "candidate_01_angle_205_raw.jpg",
            "white_wall": root / "projection_1786302247" / "candidate_01_angle_217_raw.jpg",
            "grey_cabinet": root / "projection_1786302247" / "candidate_02_angle_205_raw.jpg",
        }
        if not all(path.exists() for path in samples.values()):
            self.skipTest("real projection captures are unavailable")
        results = {
            name: analyze_projection_frame(cv2.imread(str(path)), CONFIG["projection"])
            for name, path in samples.items()
        }
        # The measured projector footprint is deliberately much larger than the
        # old centre probe.  Historical white-wall captures can therefore include
        # foreground clutter and fail the conservative local heuristic; the live
        # workflow uses the required VLM over this complete calibrated region.
        self.assertFalse(results["board"]["ok"], results["board"])
        self.assertFalse(results["grey_cabinet"]["ok"], results["grey_cabinet"])
        self.assertGreater(
            results["white_wall"]["bright_neutral_ratio"],
            results["grey_cabinet"]["bright_neutral_ratio"],
        )

    def test_projection_judges_only_footprint_roi(self):
        frame = np.full((480, 640, 3), 235, np.uint8)
        # Heavy texture outside the configured projector polygon must not make
        # an otherwise clear projected area fail.
        frame[:60, ::5] = 0
        frame[-60:, ::5] = 0
        result = analyze_projection_frame(frame, CONFIG["projection"])
        polygon = np.asarray(result["footprint_polygon_px"])
        self.assertTrue(result["ok"])
        self.assertGreater(int(polygon[:, 0].min()), 0)
        self.assertLess(int(polygon[:, 0].max()), frame.shape[1])
        self.assertGreater(int(polygon[:, 1].min()), 0)
        self.assertLess(int(polygon[:, 1].max()), frame.shape[0])
        self.assertAlmostEqual(result["footprint_geometry"]["distance_m"], 2.0)
        self.assertEqual(result["roi_mode"], "measured_projection_box_with_margin")
        np.testing.assert_array_equal(
            polygon,
            np.asarray([[198, 163], [435, 163], [435, 375], [198, 375]]),
        )
        area_ratio = float(cv2.contourArea(polygon.astype(np.float32))) / (640.0 * 480.0)
        self.assertGreater(area_ratio, 0.15)
        self.assertLess(area_ratio, 0.18)

    def test_uncalibrated_mode_accepts_clear_center_despite_outer_obstacles(self):
        frame = np.zeros((480, 640, 3), np.uint8)
        frame[182:298, 249:391] = 235
        cfg = json.loads(json.dumps(CONFIG["projection"]))
        cfg["camera_projector_calibration"] = {
            "mode": "uncalibrated_center_probe",
            "calibrated": False,
            "reference_distance_m": 2.0,
            "normalized_footprint_polygon": [
                [0.39, 0.38], [0.61, 0.38], [0.61, 0.62], [0.39, 0.62],
            ],
        }
        result = analyze_projection_frame(frame, cfg)
        self.assertTrue(result["ok"])
        self.assertGreater(result["white_ratio"], 0.95)


if __name__ == "__main__":
    unittest.main()
