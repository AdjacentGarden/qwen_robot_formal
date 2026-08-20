#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent
SINGLE_FUNCTION_DIR = SKILL_DIR.parent
sys.path.insert(0, str(SINGLE_FUNCTION_DIR))

from _shared.mijia_client import (  # noqa: E402
    MijiaError,
    create_api,
    device_lock,
    env_value,
    find_device,
    load_config,
    request,
    require_float,
    require_int,
    response_error,
    response_success,
    safe_device_summary,
    validate_auth_path,
)

SKILL = "feeder_control"
ACTION_ALIASES = {
    "feed": "feed", "dispense": "feed", "投食": "feed", "喂食": "feed", "出粮": "feed",
    "status": "status", "query": "status", "状态": "status", "查询": "status",
    "check": "check", "检查": "check",
}


def emit(ok: bool, status: str, action: str, result: dict[str, Any] | None = None,
         error: str | None = None, message: str = "", started: float | None = None) -> None:
    metrics = {"ts": round(time.time(), 3)}
    if started is not None:
        metrics["elapsed_sec"] = round(time.monotonic() - started, 3)
    print(json.dumps({
        "ok": ok, "status": status, "skill": SKILL, "action": action,
        "result": result or {}, "error": error, "message": message, "metrics": metrics,
    }, ensure_ascii=False))


def settings() -> dict[str, Any]:
    config_path = Path(os.getenv("MIJIA_FEEDER_CONFIG", str(SKILL_DIR / "config.json")))
    config = load_config(config_path)
    return {
        "config_path": str(config_path),
        "auth_path": validate_auth_path(env_value(config, "auth_path", "MIJIA_AUTH_PATH")),
        "did": str(env_value(config, "did", "FEEDER_DID")),
        "siid": require_int(env_value(config, "siid", "FEEDER_SIID"), "siid"),
        "aiid": require_int(env_value(config, "aiid", "FEEDER_AIID"), "aiid"),
        "piid": require_int(env_value(config, "piid", "FEEDER_PIID"), "piid"),
        "portion_grams": require_int(env_value(config, "portion_grams", "FEEDER_PORTION_GRAMS", 10), "portion_grams"),
        "default_grams": require_int(env_value(config, "default_grams", "FEEDER_DEFAULT_GRAMS", 10), "default_grams"),
        "min_portions": require_int(env_value(config, "min_portions", "FEEDER_MIN_PORTIONS", 1), "min_portions"),
        "max_portions": require_int(env_value(config, "max_portions", "FEEDER_MAX_PORTIONS", 10), "max_portions"),
        "request_timeout_sec": require_float(env_value(config, "request_timeout_sec", "MIJIA_REQUEST_TIMEOUT_SEC", 8), "request_timeout_sec"),
        "lock_timeout_sec": require_float(env_value(config, "lock_timeout_sec", "MIJIA_LOCK_TIMEOUT_SEC", 2), "lock_timeout_sec"),
        "refresh_token": bool(config.get("refresh_token", False)),
    }


def resolve_portions(cfg: dict[str, Any], grams: int | None, portions: int | None) -> tuple[int, int]:
    if grams is not None and portions is not None:
        raise MijiaError("specify_only_one_of_grams_or_portions")
    if portions is None:
        actual_grams = cfg["default_grams"] if grams is None else require_int(grams, "grams")
        if actual_grams % cfg["portion_grams"] != 0:
            raise MijiaError(f"grams_must_be_multiple_of_{cfg['portion_grams']}")
        portions = actual_grams // cfg["portion_grams"]
    else:
        portions = require_int(portions, "portions")
        actual_grams = portions * cfg["portion_grams"]
    if not cfg["min_portions"] <= portions <= cfg["max_portions"]:
        raise MijiaError(f"portions_out_of_range:{cfg['min_portions']}..{cfg['max_portions']}")
    return portions, actual_grams


def feed(cfg: dict[str, Any], portions: int) -> Any:
    api = create_api(cfg["auth_path"], cfg["request_timeout_sec"])
    # This action is deliberately attempted once. Retrying can dispense twice.
    response = request(api, f"/v2/home/rpc/{cfg['did']}", {
        "method": "action",
        "params": {
            "did": cfg["did"], "siid": cfg["siid"], "aiid": cfg["aiid"],
            "in": [{"piid": cfg["piid"], "value": portions}],
        },
    }, refresh_token=cfg["refresh_token"])
    if not response_success(response):
        raise MijiaError(response_error(response))
    return response


def device_summary(cfg: dict[str, Any]) -> dict[str, Any]:
    api = create_api(cfg["auth_path"], cfg["request_timeout_sec"])
    return safe_device_summary(find_device(api, cfg["did"]), cfg["did"])


def require_online(cfg: dict[str, Any]) -> dict[str, Any]:
    summary = device_summary(cfg)
    if summary.get("online") is False:
        raise MijiaError("device_offline")
    return summary


def classify_control_error(exc: Exception) -> tuple[str, str]:
    """Return a stable reason without ever claiming that food was dispensed."""
    detail = str(exc or "").strip()
    value = detail.lower()
    if "device_offline" in value or "offline" in value:
        return "device_offline", "投食机当前离线，所以没有成功投喂"
    if any(token in value for token in (
        "token", "unauthorized", "forbidden", "401", "403", "auth failed",
        "auth_file_not_found", "auth_file_not_readable", "login expired",
    )):
        return "auth_expired_or_invalid", "投食机控制凭证已经过期或无效，所以没有成功投喂"
    if any(token in value for token in ("timeout", "timed out", "readtimeout", "connecttimeout")):
        return "service_timeout", "连接投食机服务超时，所以没有成功投喂"
    if any(token in value for token in (
        "urlerror", "connection refused", "connection reset", "network is unreachable",
        "name resolution", "temporary failure", "dns",
    )):
        return "network_unavailable", "当前网络无法连接投食机服务，所以没有成功投喂"
    return "control_failed", "投食机控制失败，所以没有成功投喂"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Control the configured Mijia pet feeder.")
    parser.add_argument("action", nargs="?", default="feed")
    parser.add_argument("legacy_grams", nargs="?", type=int)
    parser.add_argument("--grams", type=int)
    parser.add_argument("--portions", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    raw_action = args.action.strip()
    if raw_action.isdigit() and args.legacy_grams is None and args.grams is None:
        args.grams = int(raw_action)
        raw_action = "feed"
    action = ACTION_ALIASES.get(raw_action.lower())
    started = time.monotonic()
    if action is None:
        emit(False, "invalid_arguments", raw_action, error="unsupported_action", message="不支持这个投食操作", started=started)
        return 2
    try:
        cfg = settings()
        if action == "check":
            create_api(cfg["auth_path"], cfg["request_timeout_sec"])
            emit(True, "ready", action, {"did": cfg["did"], "config_path": cfg["config_path"], "portion_grams": cfg["portion_grams"]}, message="投食机控制配置正常", started=started)
            return 0
        if action == "status":
            if args.grams is not None or args.portions is not None or args.legacy_grams is not None:
                raise MijiaError("status_does_not_accept_amount")
            if args.dry_run:
                emit(True, "dry_run", action, {"did": cfg["did"]}, message="投食机状态查询参数校验通过", started=started)
                return 0
            with device_lock(cfg["did"], cfg["lock_timeout_sec"]):
                result = device_summary(cfg)
            message = "投食机在线" if result.get("online") is True else "投食机离线" if result.get("online") is False else "已查询投食机状态"
            emit(True, "completed", action, result, message=message, started=started)
            return 0

        grams = args.grams if args.grams is not None else args.legacy_grams
        portions, actual_grams = resolve_portions(cfg, grams, args.portions)
        result = {"did": cfg["did"], "portions": portions, "portion_grams": cfg["portion_grams"], "actual_grams": actual_grams}
        if args.dry_run:
            emit(True, "dry_run", action, result, message="投食参数校验通过", started=started)
            return 0
        with device_lock(cfg["did"], cfg["lock_timeout_sec"]):
            require_online(cfg)
            feed(cfg, portions)
        emit(True, "completed", action, result, message=f"已投食{actual_grams}克", started=started)
        return 0
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
        failure_reason, failure_message = classify_control_error(exc)
        emit(
            False,
            failure_reason,
            action,
            result={"failure_reason": failure_reason, "detail": str(exc)[:500]},
            error=error,
            message=failure_message,
            started=started,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
