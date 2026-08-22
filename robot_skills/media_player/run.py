#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "catalog.json"
STATE_PATH = ROOT / "runtime" / "state.json"
HELPER = "/usr/local/sbin/robot-media-player"


def load_catalog() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(value: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def normalize(value: str) -> str:
    return "".join(str(value or "").lower().split()).replace("《", "").replace("》", "")


def available_items(catalog: dict, kind: str) -> list[dict]:
    return [item for item in catalog.get(kind, []) if item.get("available") is True]


def find_item(catalog: dict, kind: str, requested: str) -> dict | None:
    items = catalog.get(kind, [])
    if not requested:
        available = available_items(catalog, kind)
        return available[0] if available else None
    key = normalize(requested)
    for item in items:
        aliases = [item.get("id"), item.get("title"), *(item.get("aliases") or [])]
        if key in {normalize(alias) for alias in aliases if alias}:
            return item
    return None


def helper(action: str, item_id: str | None = None) -> tuple[bool, dict]:
    command = ["sudo", "-n", HELPER, action]
    if item_id:
        command.append(item_id)
    completed = subprocess.run(command, text=True, capture_output=True, timeout=15.0, check=False)
    payload = {}
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            payload = value
            break
    ok = completed.returncode == 0 and payload.get("ok") is True
    if not payload:
        payload = {"ok": False, "error": completed.stderr.strip()[-500:] or f"helper_exit_{completed.returncode}"}
    return ok, payload


def result(ok: bool, action: str, message: str, **extra) -> int:
    payload = {
        "ok": ok,
        "status": "success" if ok else "failed",
        "skill": "media_player",
        "action": action,
        "message": message,
        **extra,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Independent robot music and entertainment video player.")
    parser.add_argument("--action", required=True, choices=(
        "play_music", "play_video", "play_movie", "pause", "resume", "next", "stop", "status", "list"
    ))
    parser.add_argument("--title", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    catalog = load_catalog()
    state = load_state()

    if args.dry_run:
        return result(True, args.action, "媒体播放器链路校验通过，但没有实际播放。", dry_run=True)

    if args.action == "list":
        music = [{"id": item["id"], "title": item["title"], "available": bool(item.get("available"))} for item in catalog.get("music", [])]
        videos = [{"id": item["id"], "title": item["title"], "available": bool(item.get("available"))} for item in catalog.get("videos", [])]
        available_music = [item["title"] for item in music if item["available"]]
        available_videos = [item["title"] for item in videos if item["available"]]
        message = "目前可播放的音乐有" + "、".join(available_music) + "；娱乐视频有" + "、".join(available_videos) + "。"
        return result(True, args.action, message, music=music, videos=videos)

    if args.action == "status":
        if state.get("status") in {"playing", "paused"}:
            verb = "正在播放" if state["status"] == "playing" else "已暂停"
            return result(True, args.action, f"播放器{verb}《{state.get('title', '当前媒体')}》。", player_state=state)
        return result(True, args.action, "播放器当前没有在播放内容。", player_state={"status": "stopped"})

    if args.action in {"play_music", "play_video", "play_movie"}:
        kind = "music" if args.action == "play_music" else "videos"
        item = find_item(catalog, kind, args.title)
        if item is None:
            label = "歌曲" if kind == "music" else "视频"
            return result(False, args.action, f"媒体库里没有找到你说的{label}，所以没有开始播放。", error="media_not_found")
        if item.get("available") is not True:
            return result(
                False,
                args.action,
                f"《{item['title']}》目前没有已授权的本地音频文件，所以我不能播放它。你可以把合法音频放进媒体库后再试。",
                error="media_file_unavailable",
                requested=item,
            )
        helper_action = (
            "play-audio" if kind == "music"
            else "play-video-projection" if args.action == "play_movie"
            else "play-video"
        )
        ok, details = helper(helper_action, str(item["id"]))
        if not ok:
            return result(False, args.action, "播放器这次没有成功启动，所以没有开始播放。", error=details.get("error"), helper=details)
        state = {
            "status": "playing",
            "kind": kind,
            "id": item["id"],
            "title": item["title"],
            "projection": args.action == "play_movie",
        }
        save_state(state)
        return result(True, args.action, f"现在开始播放《{item['title']}》。", player_state=state)

    if args.action == "next":
        music = available_items(catalog, "music")
        if not music:
            return result(False, args.action, "媒体库里暂时没有可切换的音乐。", error="no_available_music")
        current = next((index for index, item in enumerate(music) if item["id"] == state.get("id")), -1)
        item = music[(current + 1) % len(music)]
        ok, details = helper("play-audio", str(item["id"]))
        if not ok:
            return result(False, args.action, "下一首这次没有成功启动。", error=details.get("error"), helper=details)
        state = {"status": "playing", "kind": "music", "id": item["id"], "title": item["title"]}
        save_state(state)
        return result(True, args.action, f"已经切换到《{item['title']}》。", player_state=state)

    if args.action in {"pause", "resume", "stop"}:
        if args.action != "stop" and state.get("status") not in {"playing", "paused"}:
            return result(False, args.action, "当前没有正在播放的内容。", error="nothing_playing")
        ok, details = helper(args.action)
        if not ok:
            return result(False, args.action, "播放器控制这次没有成功。", error=details.get("error"), helper=details)
        if args.action == "pause":
            state["status"] = "paused"
            message = f"《{state.get('title', '当前内容')}》已经暂停。"
        elif args.action == "resume":
            state["status"] = "playing"
            message = f"继续播放《{state.get('title', '当前内容')}》。"
        else:
            title = state.get("title")
            state = {"status": "stopped"}
            message = f"《{title}》已经结束播放。" if title else "播放已经结束。"
        save_state(state)
        return result(True, args.action, message, player_state=state)

    return result(False, args.action, "不支持这个播放器操作。", error="unsupported_action")


if __name__ == "__main__":
    raise SystemExit(main())
