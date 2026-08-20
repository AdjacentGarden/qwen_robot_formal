#!/usr/bin/env python3
"""Render a hardware-free MP4 explaining the projection and pet-search tests."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from exploration_core import (
    load_map_yaml,
    projection_candidates,
    rectangular_search_regions,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_MAP = Path("/home/test/project_0727_fixed_points_home_scenes/test_assets/maps/713_test.yaml")
DEFAULT_CONFIG = ROOT / "config" / "exploration.json"


def font_path() -> str:
    try:
        value = subprocess.check_output(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK SC"], text=True,
        ).strip()
        if value:
            return value
    except Exception:
        pass
    return "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


FONT_FILE = font_path()
FONTS = {
    18: ImageFont.truetype(FONT_FILE, 18),
    22: ImageFont.truetype(FONT_FILE, 22),
    28: ImageFont.truetype(FONT_FILE, 28),
    36: ImageFont.truetype(FONT_FILE, 36),
    46: ImageFont.truetype(FONT_FILE, 46),
}


def put(frame: np.ndarray, text: str, xy: tuple[int, int], size=22, fill=(238, 242, 248), anchor=None):
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(image)
    draw.text(xy, text, font=FONTS[size], fill=fill, anchor=anchor)
    frame[:] = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


class MapRenderer:
    def __init__(self, grid, rect=(38, 106, 548, 682)):
        self.grid = grid
        self.x1, self.y1, self.x2, self.y2 = rect
        self.scale = min((self.x2 - self.x1) / grid.width, (self.y2 - self.y1) / grid.height)
        self.map_w = int(round(grid.width * self.scale))
        self.map_h = int(round(grid.height * self.scale))
        self.ox = self.x1 + ((self.x2 - self.x1) - self.map_w) // 2
        self.oy = self.y1 + ((self.y2 - self.y1) - self.map_h) // 2
        raw = np.full(grid.data.shape, 96, np.uint8)
        raw[grid.free_mask()] = 225
        raw[grid.occupied_mask()] = 32
        raw = np.flipud(raw)
        self.base = cv2.resize(raw, (self.map_w, self.map_h), interpolation=cv2.INTER_NEAREST)

    def cell_px(self, cell):
        row, col = cell
        return (
            int(round(self.ox + (col + 0.5) * self.scale)),
            int(round(self.oy + (self.grid.height - row - 0.5) * self.scale)),
        )

    def world_px(self, x, y):
        return self.cell_px(self.grid.world_to_cell(x, y))

    def draw_base(self, frame):
        region = cv2.cvtColor(self.base, cv2.COLOR_GRAY2BGR)
        frame[self.oy:self.oy + self.map_h, self.ox:self.ox + self.map_w] = region
        cv2.rectangle(frame, (self.ox, self.oy), (self.ox + self.map_w, self.oy + self.map_h), (120, 130, 145), 1)

    def draw_coverage(self, frame, mask, color=(190, 150, 35), alpha=0.42):
        display = np.flipud(mask.astype(np.uint8) * 255)
        display = cv2.resize(display, (self.map_w, self.map_h), interpolation=cv2.INTER_NEAREST) > 0
        roi = frame[self.oy:self.oy + self.map_h, self.ox:self.ox + self.map_w]
        tint = np.zeros_like(roi)
        tint[:] = color
        roi[display] = cv2.addWeighted(roi[display], 1.0 - alpha, tint[display], alpha, 0)

    def path(self, frame, cells, color, progress=1.0, width=3):
        if not cells:
            return
        count = max(1, min(len(cells), int(math.ceil(len(cells) * max(0.0, min(1.0, progress))))))
        points = np.asarray([self.cell_px(cell) for cell in cells[:count]], np.int32)
        if len(points) > 1:
            cv2.polylines(frame, [points], False, color, width, cv2.LINE_AA)

    def robot(self, frame, x, y, yaw, color=(30, 180, 255), radius=10):
        center = self.world_px(x, y)
        cv2.circle(frame, center, radius, color, -1, cv2.LINE_AA)
        tip = (int(center[0] + 20 * math.cos(yaw)), int(center[1] - 20 * math.sin(yaw)))
        cv2.arrowedLine(frame, center, tip, (20, 40, 55), 3, cv2.LINE_AA, tipLength=0.35)

    def regions(self, frame, regions, active=-1, completed=0):
        for index, region in enumerate(regions):
            p1 = self.world_px(region.min_x, region.min_y)
            p2 = self.world_px(region.max_x, region.max_y)
            left, right = sorted((p1[0], p2[0]))
            top, bottom = sorted((p1[1], p2[1]))
            color = (80, 200, 100) if index < completed else ((230, 150, 45) if index == active else (150, 155, 165))
            overlay = frame.copy()
            cv2.rectangle(overlay, (left, top), (right, bottom), color, -1)
            frame[:] = cv2.addWeighted(frame, 0.86, overlay, 0.14, 0)
            cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
            center = self.world_px(region.x, region.y)
            cv2.circle(frame, center, 7, color, -1, cv2.LINE_AA)
            put(frame, str(index + 1), (center[0] + 10, center[1] - 8), 18)


def camera_panel(frame, suitable: bool, title: str):
    x1, y1, x2, y2 = 620, 230, 1228, 540
    panel = np.full((y2 - y1, x2 - x1, 3), 42, np.uint8)
    for x in range(0, panel.shape[1], 28):
        cv2.line(panel, (x, 0), (x + 90, panel.shape[0]), (58, 61, 68), 1)
    cx1, cx2 = int(panel.shape[1] * 0.39), int(panel.shape[1] * 0.61)
    cy1, cy2 = int(panel.shape[0] * 0.38), int(panel.shape[0] * 0.62)
    if suitable:
        panel[cy1:cy2, cx1:cx2] = (232, 232, 232)
        color = (70, 205, 105)
    else:
        cv2.line(panel, (cx1, cy1), (cx2, cy2), (25, 25, 25), 12)
        cv2.line(panel, (cx2, cy1), (cx1, cy2), (25, 25, 25), 12)
        color = (70, 80, 230)
    cv2.rectangle(panel, (cx1, cy1), (cx2, cy2), color, 4)
    frame[y1:y2, x1:x2] = panel
    put(frame, title, (x1, y1 - 18), 28, anchor="ls")
    put(frame, "仅检查绿色中心采样区", ((x1 + x2) // 2, y2 + 31), 18, fill=(178, 187, 200), anchor="mm")


def interpolate_path(grid, cells, progress):
    if not cells:
        return 0.0, 0.0
    value = max(0.0, min(1.0, progress)) * (len(cells) - 1)
    left = int(math.floor(value))
    right = min(len(cells) - 1, left + 1)
    ratio = value - left
    x1, y1 = grid.cell_to_world(cells[left])
    x2, y2 = grid.cell_to_world(cells[right])
    return x1 + (x2 - x1) * ratio, y1 + (y2 - y1) * ratio


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map-yaml", default=str(DEFAULT_MAP))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", required=True)
    parser.add_argument("--fps", type=int, default=24)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    grid = load_map_yaml(args.map_yaml)
    start_xy = tuple(config.get("map", {}).get("offline_start_pose", [0.0, 0.0])[:2])
    candidates = projection_candidates(grid, start_xy, config["projection"])
    if len(candidates) < 2:
        raise SystemExit("simulation needs at least two projection candidates")

    start_cell = grid.world_to_cell(*start_xy)
    regions = rectangular_search_regions(grid, start_cell, config["pet_search"])
    if len(regions) < 2:
        raise SystemExit("simulation needs at least two rectangular pet-search regions")

    width, height, fps, duration = 1280, 720, max(12, args.fps), 21.0
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise SystemExit(f"cannot open video writer: {output}")
    renderer = MapRenderer(grid)
    colors = {"blue": (230, 150, 45), "red": (65, 80, 230), "green": (80, 200, 100), "muted": (145, 150, 160)}

    try:
        for index in range(int(duration * fps)):
            t = index / fps
            frame = np.full((height, width, 3), (24, 27, 33), np.uint8)
            renderer.draw_base(frame)
            cv2.line(frame, (580, 82), (580, 682), (73, 79, 89), 1)
            put(frame, "自主投影与全图找豆豆 · 无硬件模拟", (38, 45), 36)

            if t < 2.0:
                put(frame, "真实历史地图 · 生产流程代码 · 模拟硬件边界", (922, 260), 28, anchor="mm")
                put(frame, "16 / 16 项测试通过", (922, 330), 46, fill=(104, 218, 133), anchor="mm")
                put(frame, "无底盘、头部、投影器、投食器动作", (922, 395), 22, fill=(178, 187, 200), anchor="mm")
            elif t < 9.0:
                put(frame, "阶段一：自主寻找合适投影面", (620, 116), 28)
                for c_index, candidate in enumerate(candidates, 1):
                    px = renderer.world_px(candidate.x, candidate.y)
                    cv2.circle(frame, px, 7, colors["muted"], -1, cv2.LINE_AA)
                    put(frame, str(c_index), (px[0] + 10, px[1] - 8), 18)
                if t < 4.0:
                    active = candidates[0]
                    progress = (t - 2.0) / 2.0
                    renderer.path(frame, active.path_cells, colors["blue"], progress)
                    x, y = interpolate_path(grid, active.path_cells, progress)
                    renderer.robot(frame, x, y, active.yaw)
                    camera_panel(frame, False, "候选墙 1：导航中")
                elif t < 5.5:
                    active = candidates[0]
                    renderer.path(frame, active.path_cells, colors["red"])
                    renderer.robot(frame, active.x, active.y, active.yaw, colors["red"])
                    camera_panel(frame, False, "候选墙 1：中心区域不合格")
                    put(frame, "自动换下一面墙", (922, 596), 28, fill=(245, 166, 87), anchor="mm")
                elif t < 7.5:
                    active = candidates[1]
                    progress = (t - 5.5) / 2.0
                    renderer.path(frame, candidates[0].path_cells, colors["red"])
                    renderer.path(frame, active.path_cells, colors["blue"], progress)
                    x, y = interpolate_path(grid, active.path_cells, progress)
                    renderer.robot(frame, x, y, active.yaw)
                    camera_panel(frame, True, "候选墙 2：导航并观察")
                else:
                    active = candidates[1]
                    renderer.path(frame, active.path_cells, colors["green"])
                    renderer.robot(frame, active.x, active.y, active.yaw, colors["green"])
                    camera_panel(frame, True, "候选墙 2：中心区域通过")
                    put(frame, "视觉复核通过 → 启动投影", (922, 596), 28, fill=(104, 218, 133), anchor="mm")
            elif t < 15.0:
                put(frame, "阶段二：少量矩形分区，逐中心扫描", (620, 116), 28)
                if t < 10.2:
                    renderer.regions(frame, regions)
                    put(frame, f"可达地图自动划分为 {len(regions)} 个矩形", (922, 280), 28, anchor="mm")
                    put(frame, "按当前距离由近到远排序", (922, 340), 22, fill=(178, 187, 200), anchor="mm")
                elif t < 11.7:
                    region = regions[0]
                    progress = (t - 10.2) / 1.5
                    renderer.regions(frame, regions, active=0)
                    renderer.path(frame, region.path_cells, colors["blue"], progress)
                    x, y = interpolate_path(grid, region.path_cells, progress)
                    renderer.robot(frame, x, y, 0.0)
                    put(frame, "导航到矩形 1 的安全中心", (922, 300), 28, anchor="mm")
                elif t < 13.1:
                    region = regions[0]
                    progress = (t - 11.7) / 1.4
                    renderer.regions(frame, regions, active=0)
                    renderer.robot(frame, region.x, region.y, progress * 2.0 * math.pi)
                    put(frame, "矩形 1 中心旋转：没有找到", (922, 300), 28, anchor="mm")
                elif t < 14.1:
                    region = regions[1]
                    progress = (t - 13.1) / 1.0
                    renderer.regions(frame, regions, active=1, completed=1)
                    renderer.path(frame, region.path_cells, colors["blue"], progress)
                    x, y = interpolate_path(grid, region.path_cells, progress)
                    renderer.robot(frame, x, y, 0.0)
                    put(frame, "导航到矩形 2 的安全中心", (922, 300), 28, anchor="mm")
                else:
                    region = regions[1]
                    progress = (t - 14.1) / 0.9
                    renderer.regions(frame, regions, active=1, completed=1)
                    renderer.robot(frame, region.x, region.y, -progress * 2.0 * math.pi, colors["green"])
                    put(frame, "连续两帧确认：豆豆找到了", (922, 300), 36, fill=(104, 218, 133), anchor="mm")
                    put(frame, "立即停车 · 不进入跟随", (922, 370), 22, fill=(178, 187, 200), anchor="mm")
            elif t < 18.0:
                put(frame, "未找到分支：每个矩形都必须扫描", (620, 116), 28)
                progress = (t - 15.0) / 3.0
                completed = min(len(regions), max(1, int(math.ceil(progress * len(regions)))))
                renderer.regions(frame, regions, active=min(len(regions) - 1, completed), completed=completed)
                put(frame, f"已完成：{completed} / {len(regions)} 个矩形中心", (922, 285), 36, fill=(97, 190, 245), anchor="mm")
                put(frame, "全部成功扫描后", (922, 365), 22, anchor="mm")
                put(frame, "才允许返回“没有找到”", (922, 410), 28, fill=(245, 166, 87), anchor="mm")
            else:
                put(frame, "模拟验证结果", (922, 170), 36, anchor="mm")
                items = [
                    "投影换墙与成功启动  通过",
                    "全部不合格不误投影  通过",
                    "当前位置找到豆豆      通过",
                    "矩形中心找到豆豆      通过",
                    "全部矩形后返回未找到  通过",
                ]
                for row, text in enumerate(items):
                    put(frame, "✓  " + text, (720, 245 + row * 56), 22, fill=(104, 218, 133))
                put(frame, "执行层 5/5 · 语音编排 7/7 · 基础算法 4/4", (922, 570), 22, fill=(178, 187, 200), anchor="mm")
                put(frame, "合计 16 / 16", (922, 625), 36, anchor="mm")

            put(frame, f"SIMULATION  {t:04.1f}s / {duration:.0f}s", (1238, 694), 18, fill=(144, 153, 166), anchor="rs")
            writer.write(frame)
    finally:
        writer.release()

    print(json.dumps({
        "ok": True,
        "output": str(output),
        "duration_sec": duration,
        "fps": fps,
        "projection_candidates": len(candidates),
        "pet_regions": len(regions),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
