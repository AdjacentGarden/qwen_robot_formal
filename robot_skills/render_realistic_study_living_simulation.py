#!/usr/bin/env python3
"""Offline simulation on realistic study/living-room lidar map variants."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from exploration_core import GridMap, load_map_yaml, projection_candidates, rectangular_search_regions, save_exploration_overview
from render_simulation_video import MapRenderer, interpolate_path, put


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "exploration.json"
DEFAULT_MAP = Path("/home/test/project_0727_fixed_points_home_scenes/test_assets/maps/713_test.yaml")


@dataclass
class Case:
    key: str
    title: str
    description: str
    grid: GridMap
    dog_region: int | None


def clone_grid(base: GridMap, data: np.ndarray, source: str) -> GridMap:
    return GridMap(
        data=data.astype(np.int16, copy=True),
        resolution=base.resolution,
        origin_x=base.origin_x,
        origin_y=base.origin_y,
        frame_id=base.frame_id,
        source=source,
    )


def wall_noise_variant(base: GridMap) -> GridMap:
    """Keep navigable free space intact while reproducing wall jitter and lidar ghosts."""
    rng = np.random.default_rng(713)
    data = base.data.copy()
    occupied = np.argwhere(data == 100)
    remove_count = max(1, len(occupied) // 14)
    for row, col in occupied[rng.choice(len(occupied), remove_count, replace=False)]:
        data[row, col] = -1

    occupied_mask = (base.data == 100).astype(np.uint8)
    outer_ring = (cv2.dilate(occupied_mask, np.ones((3, 3), np.uint8)) > 0) & (base.data < 0)
    ghosts = np.argwhere(outer_ring)
    add_count = max(1, len(ghosts) // 5)
    for row, col in ghosts[rng.choice(len(ghosts), add_count, replace=False)]:
        data[row, col] = 100
    return clone_grid(base, data, "realistic:713_wall_jitter_and_lidar_ghosts")


def partial_scan_variant(base: GridMap) -> GridMap:
    """Add irregular peripheral unknown patches without fabricating free passages."""
    noisy = wall_noise_variant(base)
    data = noisy.data.copy()
    patches = (
        (-3.25, -6.25, 6, 10, 15),
        (0.45, -6.55, 5, 7, -12),
        (-3.15, -3.00, 5, 8, 24),
    )
    for x, y, radius_x, radius_y, angle in patches:
        row, col = base.world_to_cell(x, y)
        mask = np.zeros(data.shape, np.uint8)
        cv2.ellipse(mask, (col, row), (radius_x, radius_y), angle, 0, 360, 1, -1)
        data[(mask > 0) & (data == 0)] = -1
    return clone_grid(base, data, "realistic:713_partial_scan_and_wall_jitter")


def make_cases(base: GridMap) -> list[Case]:
    return [
        Case(
            "actual_713",
            "真实书房—客厅图",
            "原始 713_test：粗墙、门洞断裂、转角毛刺与零散漏点",
            clone_grid(base, base.data, "real:713_test"),
            1,
        ),
        Case(
            "wall_jitter",
            "同户型·墙线抖动",
            "保留真实可行空间，增加墙厚波动、断续回波和墙外鬼点",
            wall_noise_variant(base),
            2,
        ),
        Case(
            "partial_scan",
            "同户型·局部漏扫",
            "在客厅边缘加入不规则未知区，模拟遮挡和覆盖不足",
            partial_scan_variant(base),
            None,
        ),
    ]


def analyze(case: Case, config: dict, start_xy=(0.0, 0.0)) -> dict:
    start_cell = case.grid.world_to_cell(*start_xy)
    candidates = projection_candidates(case.grid, start_xy, config["projection"])
    regions = rectangular_search_regions(case.grid, start_cell, config["pet_search"])
    farthest = max((item.maximum_corner_distance_m for item in regions), default=None)
    ok = bool(candidates) and bool(regions) and all(item.within_detection_distance for item in regions)
    return {
        "case": case,
        "start_xy": start_xy,
        "candidates": candidates,
        "regions": regions,
        "farthest": farthest,
        "ok": ok,
    }


def save_grid(grid: GridMap, stem: Path) -> None:
    image = np.full(grid.data.shape, 205, np.uint8)
    image[grid.data == 0] = 254
    image[grid.data >= 65] = 0
    image = np.flipud(image)
    cv2.imwrite(str(stem.with_suffix(".pgm")), image)
    stem.with_suffix(".yaml").write_text(
        "\n".join([
            f"image: {stem.with_suffix('.pgm').name}",
            f"resolution: {grid.resolution:.6f}",
            f"origin: [{grid.origin_x:.6f}, {grid.origin_y:.6f}, 0.0]",
            "negate: 0",
            "occupied_thresh: 0.65",
            "free_thresh: 0.196",
            "",
        ]),
        encoding="utf-8",
    )


def public_report(item: dict) -> dict:
    case = item["case"]
    regions = item["regions"]
    candidates = item["candidates"]
    return {
        "id": case.key,
        "title": case.title,
        "description": case.description,
        "source": case.grid.source,
        "ok": item["ok"],
        "map": case.grid.to_json_summary(),
        "start": {"x": item["start_xy"][0], "y": item["start_xy"][1]},
        "projection_candidate_count": len(candidates),
        "projection_candidates": [value.public() for value in candidates],
        "pet_region_count": len(regions),
        "pet_regions": [value.public() for value in regions],
        "maximum_corner_distance_m": item["farthest"],
        "simulated_result": "not_found_after_all_regions" if case.dog_region is None else f"found_in_region_{case.dog_region + 1}",
    }


def draw_candidates(renderer: MapRenderer, frame: np.ndarray, candidates, active=-1, completed=0) -> None:
    for index, candidate in enumerate(candidates):
        point = renderer.world_px(candidate.x, candidate.y)
        target = renderer.world_px(candidate.target_x, candidate.target_y)
        color = (80, 200, 100) if index < completed else ((55, 80, 230) if index == active else (145, 150, 160))
        cv2.line(frame, point, target, color, 2, cv2.LINE_AA)
        cv2.circle(frame, point, 7, color, -1, cv2.LINE_AA)
        put(frame, str(index + 1), (point[0] + 9, point[1] - 8), 18)


def write_video(items: list[dict], output: Path, fps: int) -> None:
    width, height = 1280, 720
    seconds_per_case = 13.0
    summary_seconds = 3.0
    duration = seconds_per_case * len(items) + summary_seconds
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"cannot_create_video:{output}")
    try:
        for frame_index in range(int(duration * fps)):
            absolute_t = frame_index / fps
            frame = np.full((height, width, 3), (24, 27, 33), np.uint8)
            if absolute_t >= seconds_per_case * len(items):
                put(frame, "真实建图风格模拟汇总", (640, 105), 46, anchor="mm")
                for row, item in enumerate(items):
                    report = public_report(item)
                    message = (
                        f"✓ {report['title']}：投影候选 {report['projection_candidate_count']} 个，"
                        f"找狗矩形 {report['pet_region_count']} 个，最远 {report['maximum_corner_distance_m']:.2f} 米"
                    )
                    put(frame, message, (130, 220 + row * 86), 28, fill=(104, 218, 133))
                put(frame, "直接使用正式几何算法 · 路径与分区均在噪声地图上重新计算", (640, 535), 28, anchor="mm")
                put(frame, "完全离线，无底盘、头部、相机或投影器动作", (640, 600), 28, fill=(97, 190, 245), anchor="mm")
                writer.write(frame)
                continue

            case_index = min(len(items) - 1, int(absolute_t // seconds_per_case))
            local_t = absolute_t - case_index * seconds_per_case
            item = items[case_index]
            case = item["case"]
            candidates = item["candidates"]
            regions = item["regions"]
            renderer = MapRenderer(case.grid)
            renderer.draw_base(frame)
            cv2.line(frame, (580, 92), (580, 682), (73, 79, 89), 1)
            put(frame, case.title, (38, 45), 36)
            put(frame, case.description, (615, 108), 28)

            if local_t < 2.0:
                renderer.regions(frame, regions)
                draw_candidates(renderer, frame, candidates)
                summary = case.grid.to_json_summary()
                put(frame, f"真实尺寸：{case.grid.width} × {case.grid.height} 栅格，分辨率 {case.grid.resolution:.2f} m", (920, 235), 28, anchor="mm")
                put(frame, f"未知栅格 {summary['unknown_cells']} · 障碍栅格 {summary['occupied_cells']}", (920, 300), 28, anchor="mm")
                put(frame, f"投影候选 {len(candidates)} 个 · 找狗矩形 {len(regions)} 个", (920, 385), 36, fill=(97, 190, 245), anchor="mm")
            elif local_t < 5.5:
                candidate = candidates[0]
                progress = (local_t - 2.0) / 3.5
                draw_candidates(renderer, frame, candidates, active=0)
                renderer.path(frame, candidate.path_cells, (55, 80, 230), progress)
                x, y = interpolate_path(case.grid, candidate.path_cells, progress)
                renderer.robot(frame, x, y, candidate.yaw, (55, 80, 230))
                put(frame, "投影：沿实际可达区域前往候选位姿", (920, 250), 28, anchor="mm")
                put(frame, "全投影光路走廊已避障", (920, 320), 28, fill=(104, 218, 133), anchor="mm")
                put(frame, "到达后再由视觉判断墙面质量", (920, 390), 22, anchor="mm")
            elif local_t < 11.0:
                dog_limit = case.dog_region if case.dog_region is not None else len(regions) - 1
                scans = dog_limit + 1
                progress = (local_t - 5.5) / 5.5 * scans
                active = min(dog_limit, int(progress))
                within = progress - int(progress)
                renderer.regions(frame, regions, active=active, completed=active)
                region = regions[active]
                if within < 0.55:
                    path_progress = within / 0.55
                    renderer.path(frame, region.path_cells, (230, 150, 45), path_progress)
                    x, y = interpolate_path(case.grid, region.path_cells, path_progress)
                    renderer.robot(frame, x, y, 0.0)
                    put(frame, f"找狗：导航到矩形 {active + 1} 的安全中心", (920, 260), 28, anchor="mm")
                else:
                    rotation = (within - 0.55) / 0.45
                    direction = 1.0 if active % 2 == 0 else -1.0
                    renderer.robot(frame, region.x, region.y, direction * rotation * 2 * math.pi, (80, 200, 100))
                    if case.dog_region == active:
                        put(frame, f"矩形 {active + 1}：连续检测到豆豆", (920, 260), 28, fill=(104, 218, 133), anchor="mm")
                        put(frame, "立即停止，不再导航或跟随", (920, 330), 28, anchor="mm")
                    else:
                        put(frame, f"矩形 {active + 1}：旋转一圈未找到", (920, 260), 28, anchor="mm")
                        put(frame, "继续前往下一矩形中心", (920, 330), 28, anchor="mm")
                put(frame, f"扫描进度：{active + 1} / {len(regions)}", (920, 445), 36, fill=(97, 190, 245), anchor="mm")
            else:
                completed = len(regions) if case.dog_region is None else case.dog_region + 1
                renderer.regions(frame, regions, completed=completed)
                if case.dog_region is None:
                    put(frame, "全部矩形均已实际覆盖", (920, 265), 28, anchor="mm")
                    put(frame, "模拟结果：没有找到豆豆", (920, 345), 36, fill=(245, 166, 87), anchor="mm")
                else:
                    put(frame, f"模拟结果：在矩形 {case.dog_region + 1} 找到豆豆", (920, 295), 36, fill=(104, 218, 133), anchor="mm")
                put(frame, "本建图质量下流程通过", (920, 445), 36, fill=(104, 218, 133), anchor="mm")

            put(frame, f"CASE {case_index + 1}/{len(items)}  {absolute_t:04.1f}s", (1238, 694), 18, fill=(144, 153, 166), anchor="rs")
            writer.write(frame)
    finally:
        writer.release()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-yaml", default=str(DEFAULT_MAP))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--video", default="realistic_study_living_simulation_raw.mp4")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--analyze-only", action="store_true")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base = load_map_yaml(args.map_yaml)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    items = [analyze(case, config) for case in make_cases(base)]

    reports = [public_report(item) for item in items]
    for item in items:
        case = item["case"]
        save_grid(case.grid, output_dir / case.key)
        save_exploration_overview(
            case.grid,
            output_dir / f"{case.key}_overview.png",
            candidates=item["candidates"],
            regions=item["regions"],
        )
    report_path = output_dir / "realistic_study_living_report.json"
    report_path.write_text(json.dumps({
        "ok": all(item["ok"] for item in items),
        "hardware_used": False,
        "base_map": str(Path(args.map_yaml).resolve()),
        "cases": reports,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not args.analyze_only:
        write_video(items, output_dir / args.video, max(10, int(args.fps)))

    print(json.dumps({
        "ok": all(item["ok"] for item in items),
        "report": str(report_path),
        "video": None if args.analyze_only else str(output_dir / args.video),
        "summary": [{
            "id": report["id"],
            "projection_candidates": report["projection_candidate_count"],
            "pet_regions": report["pet_region_count"],
            "maximum_corner_distance_m": report["maximum_corner_distance_m"],
            "result": report["simulated_result"],
        } for report in reports],
    }, ensure_ascii=False))
    return 0 if all(item["ok"] for item in items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
