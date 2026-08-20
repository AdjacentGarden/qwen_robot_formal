#!/usr/bin/env python3
"""Generate and execute several complex-map simulations without robot hardware."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from exploration_core import (
    GridMap,
    projection_candidates,
    rectangular_search_regions,
    save_exploration_overview,
)
from render_simulation_video import MapRenderer, interpolate_path, put


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "exploration.json"


@dataclass
class Scenario:
    key: str
    title: str
    description: str
    grid: GridMap
    start_xy: tuple[float, float]
    dog_region: int | None


def carve(mask: np.ndarray, r1: int, c1: int, r2: int, c2: int) -> None:
    mask[max(0, r1):min(mask.shape[0], r2), max(0, c1):min(mask.shape[1], c2)] = 1


def block(mask: np.ndarray, r1: int, c1: int, r2: int, c2: int) -> None:
    mask[max(0, r1):min(mask.shape[0], r2), max(0, c1):min(mask.shape[1], c2)] = 0


def make_grid(free: np.ndarray, name: str, resolution=0.05) -> GridMap:
    kernel = np.ones((3, 3), np.uint8)
    boundary = (cv2.dilate(free, kernel, iterations=2) > 0) & (free == 0)
    data = np.full(free.shape, -1, np.int16)
    data[boundary] = 100
    data[free > 0] = 0
    height, width = free.shape
    return GridMap(
        data=data,
        resolution=resolution,
        origin_x=-width * resolution * 0.5,
        origin_y=-height * resolution * 0.5,
        frame_id="map",
        source=f"synthetic:{name}",
    )


def scenario_apartment() -> Scenario:
    free = np.zeros((150, 200), np.uint8)
    carve(free, 12, 12, 70, 90)
    carve(free, 12, 110, 70, 188)
    carve(free, 84, 12, 138, 188)
    carve(free, 38, 88, 48, 112)
    carve(free, 67, 44, 88, 58)
    carve(free, 67, 144, 88, 158)
    block(free, 99, 73, 112, 105)
    block(free, 115, 133, 127, 154)
    grid = make_grid(free, "multi_room_apartment")
    return Scenario(
        "apartment", "复杂地图 A：多房间户型", "三个房间、两条门洞连接和室内家具障碍",
        grid, grid.cell_to_world((38, 38)), 2,
    )


def scenario_office() -> Scenario:
    free = np.zeros((180, 180), np.uint8)
    carve(free, 12, 12, 166, 75)
    carve(free, 106, 70, 166, 154)
    carve(free, 12, 96, 76, 154)
    carve(free, 48, 72, 60, 99)
    carve(free, 124, 72, 140, 98)
    block(free, 82, 30, 101, 54)
    block(free, 123, 112, 143, 133)
    block(free, 30, 121, 45, 143)
    grid = make_grid(free, "l_shaped_office")
    return Scenario(
        "office", "复杂地图 B：L形办公区", "长走廊、会议室、转角和三组内部障碍",
        grid, grid.cell_to_world((145, 42)), 1,
    )


def scenario_hall() -> Scenario:
    free = np.zeros((140, 180), np.uint8)
    carve(free, 17, 21, 123, 159)
    for r1, c1, r2, c2 in (
        (37, 42, 51, 56), (37, 122, 51, 136),
        (88, 42, 102, 56), (88, 122, 102, 136),
        (61, 78, 79, 101),
    ):
        block(free, r1, c1, r2, c2)
    grid = make_grid(free, "open_hall_with_pillars")
    return Scenario(
        "hall", "复杂地图 C：带柱开放大厅", "一个大空间、五组柱体，必须继续拆分大房间",
        grid, grid.cell_to_world((30, 28)), None,
    )


def scenarios() -> list[Scenario]:
    return [scenario_apartment(), scenario_office(), scenario_hall()]


def scenario_analysis(scenario: Scenario, config: dict) -> dict:
    start_cell = scenario.grid.world_to_cell(*scenario.start_xy)
    regions = rectangular_search_regions(scenario.grid, start_cell, config["pet_search"])
    candidates = projection_candidates(scenario.grid, scenario.start_xy, config["projection"])
    return {
        "scenario": scenario,
        "regions": regions,
        "candidates": candidates,
        "ok": bool(regions) and bool(candidates) and all(item.within_detection_distance for item in regions),
    }


def public_report(analysis: dict) -> dict:
    scenario = analysis["scenario"]
    regions = analysis["regions"]
    candidates = analysis["candidates"]
    return {
        "id": scenario.key,
        "title": scenario.title,
        "description": scenario.description,
        "ok": analysis["ok"],
        "map": scenario.grid.to_json_summary(),
        "start": {"x": scenario.start_xy[0], "y": scenario.start_xy[1]},
        "projection_candidate_count": len(candidates),
        "projection_candidates": [item.public() for item in candidates],
        "pet_region_count": len(regions),
        "pet_regions": [item.public() for item in regions],
        "all_regions_within_detection_distance": all(item.within_detection_distance for item in regions),
        "maximum_corner_distance_m": max((item.maximum_corner_distance_m for item in regions), default=None),
        "simulated_dog_region": scenario.dog_region,
        "simulated_result": "not_found_after_all_regions" if scenario.dog_region is None else f"found_in_region_{scenario.dog_region + 1}",
    }


def draw_candidates(renderer: MapRenderer, frame: np.ndarray, candidates, active=-1, completed=0):
    for index, item in enumerate(candidates):
        point = renderer.world_px(item.x, item.y)
        target = renderer.world_px(item.target_x, item.target_y)
        color = (80, 200, 100) if index < completed else ((55, 80, 230) if index == active else (145, 150, 160))
        cv2.line(frame, point, target, color, 2, cv2.LINE_AA)
        cv2.circle(frame, point, 7, color, -1, cv2.LINE_AA)
        put(frame, str(index + 1), (point[0] + 10, point[1] - 7), 18)


def write_video(analyses: list[dict], output: Path, fps: int) -> None:
    width, height = 1280, 720
    seconds_per_map = 15.0
    summary_seconds = 3.0
    duration = seconds_per_map * len(analyses) + summary_seconds
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"cannot create video:{output}")
    try:
        for frame_index in range(int(duration * fps)):
            absolute_t = frame_index / fps
            frame = np.full((height, width, 3), (24, 27, 33), np.uint8)
            if absolute_t >= seconds_per_map * len(analyses):
                put(frame, "复杂地图模拟汇总", (640, 120), 46, anchor="mm")
                for row, analysis in enumerate(analyses):
                    report = public_report(analysis)
                    text = (
                        f"✓ {report['title']}：投影候选 {report['projection_candidate_count']} 个，"
                        f"找狗矩形 {report['pet_region_count']} 个，最远 {report['maximum_corner_distance_m']:.2f} 米"
                    )
                    put(frame, text, (150, 230 + row * 82), 28, fill=(104, 218, 133))
                put(frame, "全部区域满足 2.4 米视觉距离限制 · 无硬件动作", (640, 540), 28, anchor="mm")
                put(frame, "复杂地图流程模拟完成", (640, 610), 36, fill=(104, 218, 133), anchor="mm")
                writer.write(frame)
                continue

            scenario_index = min(len(analyses) - 1, int(absolute_t // seconds_per_map))
            local_t = absolute_t - scenario_index * seconds_per_map
            analysis = analyses[scenario_index]
            scenario = analysis["scenario"]
            regions = analysis["regions"]
            candidates = analysis["candidates"]
            renderer = MapRenderer(scenario.grid)
            renderer.draw_base(frame)
            cv2.line(frame, (580, 106), (580, 682), (73, 79, 89), 1)
            put(frame, scenario.title, (38, 45), 36)
            put(frame, scenario.description, (620, 118), 28)

            if local_t < 2.0:
                renderer.regions(frame, regions)
                draw_candidates(renderer, frame, candidates)
                put(frame, f"可达自由空间：{scenario.grid.to_json_summary()['free_cells'] * scenario.grid.resolution ** 2:.1f} ㎡", (920, 245), 28, anchor="mm")
                put(frame, f"投影候选 {len(candidates)} 个 · 找狗矩形 {len(regions)} 个", (920, 315), 36, fill=(97, 190, 245), anchor="mm")
                farthest = max(item.maximum_corner_distance_m for item in regions)
                put(frame, f"区域最远距离上限：{farthest:.2f} m", (920, 390), 28, anchor="mm")
            elif local_t < 6.0:
                phase = (local_t - 2.0) / 4.0
                first = candidates[0]
                second = candidates[min(1, len(candidates) - 1)]
                if phase < 0.5:
                    progress = phase * 2.0
                    draw_candidates(renderer, frame, candidates, active=0)
                    renderer.path(frame, first.path_cells, (55, 80, 230), progress)
                    x, y = interpolate_path(scenario.grid, first.path_cells, progress)
                    renderer.robot(frame, x, y, first.yaw, (55, 80, 230))
                    put(frame, "投影候选 1：完整走廊检查通过", (920, 265), 28, anchor="mm")
                    put(frame, "模拟视觉中心区域不合格", (920, 335), 28, fill=(245, 166, 87), anchor="mm")
                else:
                    progress = (phase - 0.5) * 2.0
                    draw_candidates(renderer, frame, candidates, active=1, completed=1)
                    renderer.path(frame, second.path_cells, (80, 200, 100), progress)
                    x, y = interpolate_path(scenario.grid, second.path_cells, progress)
                    renderer.robot(frame, x, y, second.yaw, (80, 200, 100))
                    put(frame, "投影候选 2：自动换墙", (920, 265), 28, anchor="mm")
                    put(frame, "模拟中心区域与视觉复核通过", (920, 335), 28, fill=(104, 218, 133), anchor="mm")
            elif local_t < 13.0:
                dog_limit = scenario.dog_region if scenario.dog_region is not None else len(regions) - 1
                scan_total = dog_limit + 1
                progress = (local_t - 6.0) / 7.0 * scan_total
                active = min(dog_limit, int(progress))
                within = progress - int(progress)
                renderer.regions(frame, regions, active=active, completed=active)
                current = regions[active]
                if within < 0.52:
                    path_progress = within / 0.52
                    renderer.path(frame, current.path_cells, (230, 150, 45), path_progress)
                    x, y = interpolate_path(scenario.grid, current.path_cells, path_progress)
                    renderer.robot(frame, x, y, 0.0)
                    put(frame, f"导航到矩形 {active + 1} 的安全中心", (920, 270), 28, anchor="mm")
                else:
                    rotation = (within - 0.52) / 0.48
                    direction = 1.0 if active % 2 == 0 else -1.0
                    renderer.robot(frame, current.x, current.y, direction * rotation * 2.0 * math.pi, (80, 200, 100))
                    if scenario.dog_region == active:
                        put(frame, f"矩形 {active + 1}：连续两帧检测到豆豆", (920, 270), 28, fill=(104, 218, 133), anchor="mm")
                        put(frame, "立即停止后续导航", (920, 335), 28, anchor="mm")
                    else:
                        put(frame, f"矩形 {active + 1}：旋转一圈未找到", (920, 270), 28, anchor="mm")
                        put(frame, "继续下一个矩形中心", (920, 335), 28, fill=(178, 187, 200), anchor="mm")
                put(frame, f"扫描进度：{min(active + 1, len(regions))} / {len(regions)}", (920, 440), 36, fill=(97, 190, 245), anchor="mm")
            else:
                renderer.regions(frame, regions, completed=(scenario.dog_region + 1 if scenario.dog_region is not None else len(regions)))
                if scenario.dog_region is None:
                    put(frame, "所有矩形中心均已扫描", (920, 265), 28, anchor="mm")
                    put(frame, "模拟结果：没有找到豆豆", (920, 340), 36, fill=(245, 166, 87), anchor="mm")
                else:
                    put(frame, f"豆豆在矩形 {scenario.dog_region + 1} 被找到", (920, 285), 36, fill=(104, 218, 133), anchor="mm")
                    put(frame, "找到后停止，不跟随", (920, 355), 28, anchor="mm")
                put(frame, "本地图模拟通过", (920, 465), 36, fill=(104, 218, 133), anchor="mm")

            put(frame, f"MAP {scenario_index + 1}/{len(analyses)}  {absolute_t:04.1f}s", (1238, 694), 18, fill=(144, 153, 166), anchor="rs")
            writer.write(frame)
    finally:
        writer.release()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--video", default="complex_maps_simulation_raw.mp4")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--analyze-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    analyses = [scenario_analysis(item, config) for item in scenarios()]
    reports = [public_report(item) for item in analyses]
    for analysis in analyses:
        scenario = analysis["scenario"]
        save_exploration_overview(
            scenario.grid,
            output_dir / f"{scenario.key}_overview.png",
            candidates=analysis["candidates"],
            regions=analysis["regions"],
        )
    report_path = output_dir / "complex_maps_report.json"
    report_path.write_text(json.dumps({
        "ok": all(item["ok"] for item in analyses),
        "scenario_count": len(reports),
        "scenarios": reports,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.analyze_only:
        write_video(analyses, output_dir / args.video, max(10, int(args.fps)))
    print(json.dumps({
        "ok": all(item["ok"] for item in analyses),
        "report": str(report_path),
        "video": None if args.analyze_only else str(output_dir / args.video),
        "summary": [{
            "id": item["id"],
            "candidates": item["projection_candidate_count"],
            "regions": item["pet_region_count"],
            "max_distance_m": item["maximum_corner_distance_m"],
            "result": item["simulated_result"],
        } for item in reports],
    }, ensure_ascii=False))
    return 0 if all(item["ok"] for item in analyses) else 1


if __name__ == "__main__":
    raise SystemExit(main())
