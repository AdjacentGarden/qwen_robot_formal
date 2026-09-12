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

SKILL = "light_control"
ACTION_ALIASES = {
    "on": "on", "open": "on", "enable": "on", "打开": "on", "开": "on", "开灯": "on",
    "off": "off", "close": "off", "disable": "off", "关闭": "off", "关": "off", "关灯": "off",
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
    config_path = Path(os.getenv("MIJIA_LIGHT_CONFIG", str(SKILL_DIR / "config.json")))
    config = load_config(config_path)
    return {
        "config_path": str(config_path),
        "auth_path": validate_auth_path(env_value(config, "auth_path", "MIJIA_AUTH_PATH")),
        "did": str(env_value(config, "did", "FLOOR_LAMP_DID")),
        "siid": require_int(env_value(config, "siid", "FLOOR_LAMP_SIID"), "siid"),
        "piid": require_int(env_value(config, "piid", "FLOOR_LAMP_PIID"), "piid"),
        "request_timeout_sec": require_float(env_value(config, "request_timeout_sec", "MIJIA_REQUEST_TIMEOUT_SEC", 8), "request_timeout_sec"),
        "lock_timeout_sec": require_float(env_value(config, "lock_timeout_sec", "MIJIA_LOCK_TIMEOUT_SEC", 2), "lock_timeout_sec"),
        "idempotent_retries": require_int(env_value(config, "idempotent_retries", "MIJIA_LIGHT_RETRIES", 1), "idempotent_retries", 0),
        "retry_delay_sec": require_float(env_value(config, "retry_delay_sec", "MIJIA_RETRY_DELAY_SEC", 0.25), "retry_delay_sec", 0),
        "refresh_token": bool(config.get("refresh_token", False)),
    }


def set_power(cfg: dict[str, Any], enabled: bool) -> Any:
    last_error: Exception | None = None
    for attempt in range(cfg["idempotent_retries"] + 1):
        try:
            api = create_api(cfg["auth_path"], cfg["request_timeout_sec"])
            response = request(api, "/miotspec/prop/set", {"params": [{
                "did": cfg["did"], "siid": cfg["siid"], "piid": cfg["piid"], "value": enabled,
            }]}, refresh_token=cfg["refresh_token"])
            if response_success(response):
                return response
            raise MijiaError(response_error(response))
        except Exception as exc:
            last_error = exc
            if attempt >= cfg["idempotent_retries"]:
                raise
            time.sleep(cfg["retry_delay_sec"])
    raise MijiaError(str(last_error or "light_control_failed"))


def get_power(cfg: dict[str, Any]) -> tuple[bool | None, Any]:
    api = create_api(cfg["auth_path"], cfg["request_timeout_sec"])
    response = request(api, "/miotspec/prop/get", {"params": [{
        "did": cfg["did"], "siid": cfg["siid"], "piid": cfg["piid"],
    }]}, refresh_token=cfg["refresh_token"])
    if not response_success(response):
        raise MijiaError(response_error(response))
    if isinstance(response, list):
        rows = response
    elif isinstance(response, dict):
        rows = response.get("result", [])
    else:
        rows = []
    value = rows[0].get("value") if rows and isinstance(rows[0], dict) else None
    return value if isinstance(value, bool) else None, response


def device_summary(cfg: dict[str, Any]) -> dict[str, Any]:
    api = create_api(cfg["auth_path"], cfg["request_timeout_sec"])
    return safe_device_summary(find_device(api, cfg["did"]), cfg["did"])


def require_online(cfg: dict[str, Any]) -> dict[str, Any]:
    summary = device_summary(cfg)
    if summary.get("online") is False:
        raise MijiaError("device_offline")
    return summary


def classify_control_error(exc: Exception) -> tuple[str, str]:
    """Return a stable machine reason plus a truthful Chinese explanation."""
    detail = str(exc or "").strip()
    value = detail.lower()
    if "device_offline" in value or "offline" in value:
        return "device_offline", "客厅灯当前离线，所以没有成功打开"
    if any(token in value for token in (
        "token", "unauthorized", "forbidden", "401", "403", "auth failed",
        "auth_file_not_found", "auth_file_not_readable", "login expired",
    )):
        return "auth_expired_or_invalid", "客厅灯的控制凭证已经过期或无效，所以没有成功打开"
    if any(token in value for token in ("timeout", "timed out", "readtimeout", "connecttimeout")):
        return "service_timeout", "连接客厅灯服务超时，所以没有成功打开"
    if any(token in value for token in (
        "urlerror", "connection refused", "connection reset", "network is unreachable",
        "name resolution", "temporary failure", "dns",
    )):
        return "network_unavailable", "当前网络无法连接客厅灯服务，所以没有成功打开"
    return "control_failed", "客厅灯控制失败，所以没有成功打开"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Control the configured Mijia floor lamp.")
    parser.add_argument("action", nargs="?", default="status")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    action = ACTION_ALIASES.get(args.action.strip().lower())
    started = time.monotonic()
    if action is None:
        emit(False, "invalid_arguments", args.action, error="unsupported_action", message="不支持这个灯光操作", started=started)
        return 2
    try:
        cfg = settings()
        if action == "check":
            create_api(cfg["auth_path"], cfg["request_timeout_sec"])
            emit(True, "ready", action, {"did": cfg["did"], "config_path": cfg["config_path"]}, message="落地灯控制配置正常", started=started)
            return 0
        if args.dry_run:
            emit(True, "dry_run", action, {"did": cfg["did"], "requested_power": action if action in {"on", "off"} else None}, message="落地灯控制参数校验通过", started=started)
            return 0
        with device_lock(cfg["did"], cfg["lock_timeout_sec"]):
            if action == "status":
                result = device_summary(cfg)
                if result.get("online") is False:
                    # A stale cloud-side power property must never make an
                    # offline lamp sound as if it were currently on.
                    result["power"] = None
                    emit(True, "device_offline", action, result, message="客厅灯目前离线", started=started)
                    return 0
                power, _ = get_power(cfg)
                result["power"] = power
                text = "客厅灯已打开" if power is True else "客厅灯已关闭" if power is False else "已查询客厅灯状态"
            else:
                enabled = action == "on"
                require_online(cfg)
                set_power(cfg, enabled)
                result = {"did": cfg["did"], "power": enabled}
                text = "客厅灯已打开" if enabled else "客厅灯已关闭"
        emit(True, "completed", action, result, message=text, started=started)
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
