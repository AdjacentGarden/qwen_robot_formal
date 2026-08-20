#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request

SKILL = "environment_perception"
DEFAULT_PUCODING_BASE_URL = "https://pucoding.com/v1/chat/completions"
DEFAULT_PUCODING_ENV_FILE = "/home/test/.config/white_wall_pucoding.env"
DEFAULT_VLM_MODEL = "gpt-5.6-luna"
DEFAULT_VLM_FALLBACK_MODEL = ""
_RESIDENT_CAMERA_FACTORY = None


def emit(ok, status, action, result=None, error=None, metrics=None):
    print(json.dumps({
        "ok": bool(ok),
        "status": status,
        "skill": SKILL,
        "action": action,
        "result": result or {},
        "error": error,
        "metrics": metrics or {"ts": round(time.time(), 3)},
    }, ensure_ascii=False))


def capture_frame(dev, role, width=640, height=480, jpeg_quality=70, warmup_frames=5, warmup_interval=0.01):
    import cv2

    cap = _RESIDENT_CAMERA_FACTORY(dev) if callable(_RESIDENT_CAMERA_FACTORY) else cv2.VideoCapture(dev)
    if not cap.isOpened():
        return {"device": dev, "ok": False, "error": "open_failed", "frames": 0}

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    frame = None
    ok_count = 0
    try:
        frame_count = max(1, int(warmup_frames))
        for index in range(frame_count):
            ok, img = cap.read()
            if ok and img is not None:
                frame = img.copy()
                ok_count += 1
            if index + 1 < frame_count and warmup_interval > 0:
                time.sleep(float(warmup_interval))
    finally:
        cap.release()

    if frame is None:
        return {"device": dev, "ok": False, "error": "read_failed", "frames": ok_count}

    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
    if not ok:
        return {"device": dev, "ok": False, "error": "jpeg_encode_failed", "frames": ok_count}

    payload = base64.b64encode(buf.tobytes()).decode("ascii")
    return {
        "device": dev,
        "ok": True,
        "role": role,
        "frames": ok_count,
        "jpeg_quality": int(jpeg_quality),
        "jpeg_bytes": int(len(buf)),
        "image_jpeg_base64": payload,
    }


def capture_front_frame(dev, width=640, height=480, jpeg_quality=70, warmup_frames=5):
    return capture_frame(dev, "front_camera", width=width, height=height, jpeg_quality=jpeg_quality, warmup_frames=warmup_frames)


def strip_image_payload(frame):
    item = dict(frame)
    item.pop("image_jpeg_base64", None)
    return item


def load_provider_env(path=None):
    values = {}
    env_path = os.path.expanduser(path or os.getenv("PUCODING_ENV_FILE", DEFAULT_PUCODING_ENV_FILE))
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values


def get_pucoding_settings():
    provider_env = load_provider_env()
    token = os.getenv("PUCODING_API_KEY") or provider_env.get("PUCODING_API_KEY")
    base_url = (
        os.getenv("PUCODING_BASE_URL")
        or provider_env.get("PUCODING_BASE_URL")
        or DEFAULT_PUCODING_BASE_URL
    )
    model = os.getenv("PUCODING_MODEL") or provider_env.get("PUCODING_MODEL") or DEFAULT_VLM_MODEL
    return token, base_url, model


def extract_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError("model_response_did_not_contain_json")
    return json.loads(match.group(0))


def normalize_vlm_decision(raw):
    def normalize_item(value):
        value = value if isinstance(value, dict) else {}
        return {
            "ok": bool(value.get("ok", False)),
            "confidence": float(value.get("confidence", 0.0) or 0.0),
            "reasons": list(value.get("reasons", []))[:6],
            "blockers": list(value.get("blockers", []))[:6],
        }

    raw = raw if isinstance(raw, dict) else {}
    suitable = raw.get("suitable", {}) if isinstance(raw.get("suitable", {}), dict) else {}
    exercise_area = raw.get("exercise_area", {})
    lighting = raw.get("lighting", {}) if isinstance(raw.get("lighting", {}), dict) else {}
    lighting_state = str(lighting.get("state", "uncertain") or "uncertain").strip().lower()
    if lighting_state not in {"on", "off", "uncertain"}:
        lighting_state = "uncertain"
    return {
        "summary": str(raw.get("summary", ""))[:500],
        "suitable": {
            "projection": normalize_item(suitable.get("projection")),
            "push_up": normalize_item(suitable.get("push_up")),
            "squat": normalize_item(suitable.get("squat")),
            "pull_up": normalize_item(suitable.get("pull_up")),
        },
        "projection_candidates": list(raw.get("projection_candidates", []))[:5],
        "exercise_area": exercise_area if isinstance(exercise_area, dict) else {},
        "visible_people": list(raw.get("visible_people", []))[:10],
        "unsafe_reasons": list(raw.get("unsafe_reasons", []))[:10],
        "recommendations": list(raw.get("recommendations", []))[:8],
        "lighting": {
            "state": lighting_state,
            "confidence": float(lighting.get("confidence", 0.0) or 0.0),
            "reason": str(lighting.get("reason", ""))[:300],
        },
    }


def use_env_proxy_enabled(args):
    if args.use_env_proxy:
        return True
    return str(os.getenv("PUCODING_USE_ENV_PROXY", "0")).strip().lower() in {"1", "true", "yes", "on"}


def call_pucoding_vlm(purpose, frames, model, base_url, timeout, use_env_proxy=False, max_tokens=800):
    token, configured_url, configured_model = get_pucoding_settings()
    if not token:
        raise RuntimeError("missing_pucoding_token")
    base_url = str(base_url or configured_url or DEFAULT_PUCODING_BASE_URL)
    model = str(model or configured_model or DEFAULT_VLM_MODEL)
    frames = [frame for frame in frames if isinstance(frame, dict)]
    usable_frames = [frame for frame in frames if frame.get("ok") and frame.get("image_jpeg_base64")]
    if not usable_frames:
        raise RuntimeError("camera_unavailable: no usable front/back frame")
    usable_roles = {frame.get("role") for frame in usable_frames}
    if purpose == "fitness" and "back_camera" not in usable_roles:
        raise RuntimeError("back_camera_required_for_fitness")
    if purpose == "projection" and "front_camera" not in usable_roles:
        raise RuntimeError("front_camera_required_for_projection")
    if purpose == "fitness_projection" and not {"front_camera", "back_camera"}.issubset(usable_roles):
        raise RuntimeError("front_and_back_camera_required_for_fitness_projection")

    prompt = (
        "You are the visual safety and scene suitability judge for a home robot. "
        "The robot is about 30 cm tall. "
        "Images are tagged by camera role. FRONT camera is for projection/environment; "
        "configured BACK camera is for viewing the user's body during squats, push-ups, and pull-ups. "
        "Judge only from the provided images and keep front/back roles separate. "
        "Projection suitability must be judged from the FRONT camera. "
        "Fitness/body-in-frame suitability must be judged from the BACK camera when available. "
        "Decide whether the current scene is suitable for: "
        "1) projecting onto a vertical wall or projection screen in front of the robot, "
        "2) push-ups, 3) squats, 4) pull-ups. "
        "Projection can pass only if a front-facing vertical wall or screen is visible and suitable. "
        "Do not mark projection suitable because the floor, door, furniture, or side wall is visible. "
        "For pull-ups, require visible equipment such as a pull-up bar or clearly safe overhead support. "
        "Consider free floor area, obstacles, people, pets, lighting, reflections, privacy, and collision risks. "
        "All natural-language values in summary, reasons, blockers, reason, unsafe_reasons, and recommendations "
        "must be concise Simplified Chinese; never return English prose in those fields. "
        "Return compact STRICT JSON only, no markdown. Schema: "
        "{\"summary\":\"...\",\"suitable\":{\"projection\":{\"ok\":bool,\"confidence\":0-1,\"reasons\":[],\"blockers\":[]},"
        "\"push_up\":{\"ok\":bool,\"confidence\":0-1,\"reasons\":[],\"blockers\":[]},"
        "\"squat\":{\"ok\":bool,\"confidence\":0-1,\"reasons\":[],\"blockers\":[]},"
        "\"pull_up\":{\"ok\":bool,\"confidence\":0-1,\"reasons\":[],\"blockers\":[]}},"
        "\"projection_candidates\":[{\"surface\":\"front_wall|screen|floor|unknown\",\"direction\":\"front|unknown\",\"confidence\":0-1,\"reason\":\"...\"}],"
        "\"exercise_area\":{\"location_hint\":\"front|unknown\",\"confidence\":0-1,\"reason\":\"...\"},"
        "\"visible_people\":[],\"unsafe_reasons\":[],\"recommendations\":[]} "
        f"Purpose: {purpose}."
    )
    if purpose == "lighting":
        prompt = (
            "You are checking whether the living-room lamp is visibly illuminating the room after a robot "
            "attempted to switch it on. Judge only from the tagged FRONT camera image. Account for camera "
            "auto-exposure, daylight, windows and projector light. Use state=on only when there is clear visual "
            "evidence that the room lamp is illuminating the scene; use off when it clearly is not; otherwise "
            "use uncertain. Return compact STRICT JSON only, no markdown. All natural-language text must be "
            "concise Simplified Chinese. Schema: {\"summary\":\"...\",\"lighting\":{\"state\":\"on|off|uncertain\","
            "\"confidence\":0-1,\"reason\":\"...\"},\"visible_people\":[],\"unsafe_reasons\":[],"
            "\"recommendations\":[]}"
        )
    elif purpose == "projection":
        prompt = (
            "You are the projection-surface safety judge for a 30 cm tall home robot. "
            "Judge ONLY projection suitability from the tagged FRONT camera image. "
            "Do not discuss exercise, fitness, body framing, or exercise equipment. "
            "Projection passes only when a front-facing vertical wall or screen is visible and suitable; "
            "floor, doors, furniture, and side walls do not count. Consider lighting, reflections, privacy, "
            "obstructions, people, and collision risks. All natural-language values must be concise Simplified "
            "Chinese, never English. Return compact STRICT JSON only, no markdown. Schema: "
            "{\"summary\":\"...\",\"suitable\":{\"projection\":{\"ok\":bool,\"confidence\":0-1,"
            "\"reasons\":[],\"blockers\":[]}},\"projection_candidates\":[{\"surface\":"
            "\"front_wall|screen|floor|unknown\",\"direction\":\"front|unknown\",\"confidence\":0-1,"
            "\"reason\":\"...\"}],\"visible_people\":[],\"unsafe_reasons\":[],\"recommendations\":[]}"
        )
    content = [{"type": "text", "text": prompt}]
    for frame in usable_frames:
        content.append({"type": "text", "text": f"Next image role: {frame.get('role')}, device: {frame.get('device')}"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + frame["image_jpeg_base64"]}})

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": content,
        }],
        "temperature": 0.1,
        "max_tokens": int(max_tokens),
        "stream": False,
    }

    endpoints = [base_url]
    if DEFAULT_PUCODING_BASE_URL not in endpoints:
        endpoints.append(DEFAULT_PUCODING_BASE_URL)
    opener = urllib.request.build_opener() if use_env_proxy else urllib.request.build_opener(urllib.request.ProxyHandler({}))
    errors = []
    body = None
    for endpoint in endpoints:
        url = endpoint.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with opener.open(req, timeout=float(timeout)) as resp:
                body = resp.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            errors.append(f"{url}:http_{exc.code}:{detail}")
        except TimeoutError:
            errors.append(f"{url}:timeout_after_{float(timeout):.1f}s")
        except urllib.error.URLError as exc:
            errors.append(f"{url}:url_error:{repr(exc)}")
    if body is None:
        raise RuntimeError("pucoding_all_endpoints_failed:" + " | ".join(errors))

    data = json.loads(body)
    text = data["choices"][0]["message"]["content"]
    return normalize_vlm_decision(extract_json_object(text)), data.get("usage", {})


def build_vlm_result(purpose, frames, vlm_decision, usage, model, base_url):
    frames = [frame for frame in frames if isinstance(frame, dict)]
    front_frame = next((frame for frame in frames if frame.get("role") == "front_camera"), {})
    back_frame = next((frame for frame in frames if frame.get("role") == "back_camera"), {})
    suitable = vlm_decision["suitable"]
    exercise_area = vlm_decision.get("exercise_area") or {}
    fitness_ok = bool(suitable["push_up"]["ok"] or suitable["squat"]["ok"] or suitable["pull_up"]["ok"])
    fitness_candidates = [exercise_area] if exercise_area else []

    result = {
        "purpose": purpose,
        "camera_stats": [strip_image_payload(frame) for frame in frames],
        "front_camera": strip_image_payload(front_frame),
        "back_camera": strip_image_payload(back_frame),
        "camera_roles": {
            "front_camera": "projection/environment judgement",
            "back_camera": "fitness/body-in-frame judgement",
        },
        "projection_suitability": {
            "ok": bool(suitable["projection"]["ok"]),
            "target_surface": "front_wall",
            "camera": "front_camera",
            "confidence": suitable["projection"]["confidence"],
            "reasons": suitable["projection"]["reasons"],
            "blockers": suitable["projection"]["blockers"],
            "candidates": vlm_decision.get("projection_candidates", []),
        },
        "fitness_space": {"ok": fitness_ok, "camera": "back_camera" if back_frame else "front_camera", "candidates": fitness_candidates},
        "exercise_suitability": {
            "push_up": suitable["push_up"],
            "squat": suitable["squat"],
            "pull_up": suitable["pull_up"],
        },
        "visible_people": vlm_decision.get("visible_people", []),
        "unsafe_reasons": vlm_decision.get("unsafe_reasons", []),
        "recommendations": vlm_decision.get("recommendations", []),
        "model_summary": vlm_decision.get("summary", ""),
        "vlm": {
            "enabled": True,
            "provider": "pucoding",
            "model": model,
            "base_url": base_url,
            "usage": usage,
        },
        "decision_policy": "front_camera_for_projection_back_camera_for_fitness",
    }
    if purpose == "lighting":
        result.pop("projection_suitability", None)
        result.pop("fitness_space", None)
        result.pop("exercise_suitability", None)
        result["lighting_observation"] = dict(vlm_decision.get("lighting") or {})
        result["camera_roles"] = {"front_camera": "living-room lighting verification"}
        result["decision_policy"] = "front_camera_lighting_verification_only"
    elif purpose == "projection":
        result.pop("fitness_space", None)
        result.pop("exercise_suitability", None)
        result["camera_roles"] = {"front_camera": "projection/environment judgement"}
        result["decision_policy"] = "front_camera_for_projection_only"
    elif purpose == "fitness":
        result.pop("projection_suitability", None)
        result["camera_roles"] = {"back_camera": "fitness/body-in-frame judgement"}
        result["decision_policy"] = "back_camera_for_fitness_only"
    return result


def resolve_camera_plan(args):
    front, back = project_camera_defaults()
    requested = args.camera or []
    normalized = [str(item).strip() for item in requested if str(item).strip()]
    if normalized:
        if len(normalized) == 1 and normalized[0].lower() in {"both", "front+back", "front_back"}:
            return [("front_camera", front), ("back_camera", back)]
        plan = []
        for index, item in enumerate(normalized):
            low = item.lower()
            if low in {"front", "front_camera"}:
                plan.append(("front_camera", front))
            elif low in {"back", "rear", "back_camera"}:
                plan.append(("back_camera", back))
            elif item == back:
                plan.append(("back_camera", item))
            elif item == front:
                plan.append(("front_camera", item))
            else:
                plan.append(("front_camera" if index == 0 else "back_camera", item))
        return plan
    if args.purpose == "fitness":
        return [("back_camera", back)]
    if args.purpose == "fitness_projection":
        return [("front_camera", front), ("back_camera", back)]
    return [("front_camera", front)]


def project_camera_defaults():
    front = os.getenv("FRONT_CAMERA_ID")
    back = os.getenv("BACK_CAMERA_ID")
    if front and back:
        return front, back
    config_path = os.getenv("ROBOT_PROJECT_CONFIG", "/home/test/new_project/config/hardware.json")
    try:
        config = json.loads(open(config_path, "r", encoding="utf-8").read())
        cameras = config.get("cameras") or {}
        front_cfg = cameras.get("front") or {}
        back_cfg = cameras.get("back") or {}
    except Exception:
        front_cfg = {}
        back_cfg = {}
    return front or str(front_cfg.get("device") or "/dev/video22"), back or str(back_cfg.get("device") or "/dev/video22")


def main(argv=None):
    _token, configured_base_url, configured_model = get_pucoding_settings()
    parser = argparse.ArgumentParser(description="Environment perception using front/back robot camera frames and PuCoding VLM.")
    parser.add_argument("--purpose", default="general", choices=["general", "projection", "fitness", "fitness_projection", "lighting"])
    parser.add_argument("--camera", action="append", default=[], help="front, back, both, or explicit device path. Device aliases are resolved from /home/test/new_project/config/hardware.json.")
    parser.add_argument("--exercise-type", default=None, help="Optional fitness target: squat, push_up, or pull_up.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=float(os.getenv("SINGLE_FUNCTION_TIMEOUT", "12")))
    parser.add_argument("--use-vlm", action="store_true", help="Kept for compatibility; VLM is always required unless --dry-run is used.")
    parser.add_argument("--no-vlm", action="store_true", help="Unsupported now: this skill is VLM-only.")
    parser.add_argument("--strict-vlm", action="store_true", help="Kept for compatibility; VLM errors always fail now.")
    parser.add_argument("--vlm-model", default=configured_model)
    parser.add_argument("--fallback-vlm-model", default=os.getenv("PUCODING_VLM_FALLBACK_MODEL", DEFAULT_VLM_FALLBACK_MODEL))
    parser.add_argument("--vlm-base-url", default=configured_base_url)
    parser.add_argument("--vlm-timeout", type=float, default=None)
    parser.add_argument("--jpeg-quality", type=int, default=int(os.getenv("ENV_PERCEPTION_JPEG_QUALITY", "60")))
    parser.add_argument("--width", type=int, default=int(os.getenv("ENV_PERCEPTION_WIDTH", "640")))
    parser.add_argument("--height", type=int, default=int(os.getenv("ENV_PERCEPTION_HEIGHT", "480")))
    parser.add_argument("--warmup-frames", type=int, default=int(os.getenv("ENV_PERCEPTION_WARMUP_FRAMES", "3")))
    parser.add_argument("--warmup-interval", type=float, default=float(os.getenv("ENV_PERCEPTION_WARMUP_INTERVAL_SEC", "0.01")))
    parser.add_argument("--max-tokens", type=int, default=int(os.getenv("PUCODING_VLM_MAX_TOKENS", "600")))
    parser.add_argument("--use-env-proxy", action="store_true", help="allow urllib to use http_proxy/https_proxy from environment")
    args = parser.parse_args(argv)

    if args.vlm_timeout is None:
        args.vlm_timeout = float(os.getenv("PUCODING_VLM_TIMEOUT", "22"))

    camera_plan = resolve_camera_plan(args)

    start = time.time()
    try:
        if args.dry_run:
            fake_decision = normalize_vlm_decision({
                "summary": "dry run: VLM-only front camera path is reachable.",
                "lighting": {"state": "uncertain", "confidence": 0.0, "reason": "dry_run"},
                "suitable": {
                    "projection": {"ok": False, "confidence": 0.0, "reasons": [], "blockers": ["dry_run"]},
                    "push_up": {"ok": False, "confidence": 0.0, "reasons": [], "blockers": ["dry_run"]},
                    "squat": {"ok": False, "confidence": 0.0, "reasons": [], "blockers": ["dry_run"]},
                    "pull_up": {"ok": False, "confidence": 0.0, "reasons": [], "blockers": ["dry_run"]},
                },
            })
            fake_frames = [{"device": device, "ok": True, "role": role, "frames": 0, "jpeg_quality": args.jpeg_quality, "jpeg_bytes": 0} for role, device in camera_plan]
            result = build_vlm_result(args.purpose, fake_frames, fake_decision, {}, args.vlm_model, args.vlm_base_url)
            result["vlm"]["dry_run"] = True
            emit(True, "dry_run", args.purpose, result, metrics={"elapsed_sec": round(time.time() - start, 3), "vlm_used": False})
            return

        if args.no_vlm:
            raise RuntimeError("vlm_required_no_vlm_not_supported")

        capture_started = time.time()
        capture_kwargs = {
            "width": args.width,
            "height": args.height,
            "jpeg_quality": args.jpeg_quality,
            "warmup_frames": args.warmup_frames,
            "warmup_interval": args.warmup_interval,
        }
        devices = [str(device) for _role, device in camera_plan]
        if len(camera_plan) > 1 and len(set(devices)) == len(devices):
            frames = [None] * len(camera_plan)
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(camera_plan)) as pool:
                futures = {
                    pool.submit(capture_frame, device, role, **capture_kwargs): index
                    for index, (role, device) in enumerate(camera_plan)
                }
                for future in concurrent.futures.as_completed(futures):
                    frames[futures[future]] = future.result()
        else:
            frames = [
                capture_frame(device, role, **capture_kwargs)
                for role, device in camera_plan
            ]
        capture_elapsed = time.time() - capture_started
        models = [str(args.vlm_model).strip()]
        fallback_model = str(args.fallback_vlm_model or "").strip()
        if fallback_model and fallback_model not in models:
            models.append(fallback_model)
        vlm_started = time.time()
        model_errors = []
        for model_name in models:
            try:
                vlm_decision, usage = call_pucoding_vlm(
                    args.purpose,
                    frames,
                    model_name,
                    args.vlm_base_url,
                    args.vlm_timeout,
                    use_env_proxy=use_env_proxy_enabled(args),
                    max_tokens=args.max_tokens,
                )
                model_used = model_name
                break
            except Exception as model_exc:
                model_errors.append({"model": model_name, "error": repr(model_exc)})
        else:
            raise RuntimeError("all_vlm_models_failed: " + json.dumps(model_errors, ensure_ascii=False))
        result = build_vlm_result(args.purpose, frames, vlm_decision, usage, model_used, args.vlm_base_url)
        result["vlm"]["fallback_used"] = model_used != models[0]
        result["vlm"]["attempt_errors"] = model_errors
        emit(
            True,
            "done",
            args.purpose,
            result,
            metrics={
                "elapsed_sec": round(time.time() - start, 3),
                "capture_elapsed_sec": round(capture_elapsed, 3),
                "vlm_elapsed_sec": round(time.time() - vlm_started, 3),
                "vlm_used": True,
                "model_used": model_used,
            },
        )
    except Exception as exc:
        emit(False, "error", args.purpose, error=repr(exc), metrics={"elapsed_sec": round(time.time() - start, 3)})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
