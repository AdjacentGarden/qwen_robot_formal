#!/usr/bin/env python3
"""Small, defensive wrapper around the locally installed mijiaAPI package."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class MijiaError(RuntimeError):
    """A stable error type for skill-facing Mijia failures."""


_API_CACHE: dict[tuple[str, float, int], Any] = {}
_API_CACHE_LOCK = threading.RLock()


def load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MijiaError(f"config_not_found:{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise MijiaError(f"config_invalid:{path}:{exc}") from exc
    if not isinstance(value, dict):
        raise MijiaError(f"config_must_be_object:{path}")
    return value


def env_value(config: dict[str, Any], key: str, env_name: str, default: Any = None) -> Any:
    value = os.getenv(env_name)
    if value is not None and value != "":
        return value
    return config.get(key, default)


def require_int(value: Any, name: str, minimum: int = 1) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MijiaError(f"invalid_{name}:{value!r}") from exc
    if result < minimum:
        raise MijiaError(f"invalid_{name}:{result}")
    return result


def require_float(value: Any, name: str, minimum: float = 0.1) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MijiaError(f"invalid_{name}:{value!r}") from exc
    if result < minimum:
        raise MijiaError(f"invalid_{name}:{result}")
    return result


def validate_auth_path(value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_file():
        raise MijiaError(f"auth_file_not_found:{path}")
    if not os.access(path, os.R_OK):
        raise MijiaError(f"auth_file_not_readable:{path}")
    return path


def create_api(auth_path: Path, request_timeout_sec: float):
    try:
        stamp = int(auth_path.stat().st_mtime_ns)
    except OSError:
        stamp = 0
    cache_key = (str(auth_path.resolve()), float(request_timeout_sec), stamp)
    with _API_CACHE_LOCK:
        cached = _API_CACHE.get(cache_key)
        if cached is not None:
            return cached
    try:
        from mijiaAPI import mijiaAPI
    except (ImportError, ModuleNotFoundError) as exc:
        raise MijiaError("mijiaAPI_not_installed") from exc

    try:
        api = mijiaAPI(str(auth_path))
    except Exception as exc:
        raise MijiaError(f"mijia_client_init_failed:{type(exc).__name__}:{exc}") from exc

    session = getattr(api, "session", None)
    if session is not None:
        session.trust_env = False
        session.proxies = {}
        original_request = session.request

        def request_with_timeout(*args: Any, **kwargs: Any):
            kwargs.setdefault("timeout", request_timeout_sec)
            return original_request(*args, **kwargs)

        session.request = request_with_timeout
    with _API_CACHE_LOCK:
        # Discard older clients if the auth file changed.
        for key in list(_API_CACHE):
            if key[0] == cache_key[0] and key != cache_key:
                old = _API_CACHE.pop(key)
                session = getattr(old, "session", None)
                close = getattr(session, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        _API_CACHE[cache_key] = api
    return api


def request(api: Any, uri: str, payload: dict[str, Any], refresh_token: bool = False) -> Any:
    method = getattr(api, "_request", None)
    if not callable(method):
        raise MijiaError("mijia_private_request_api_unavailable")
    try:
        return method(uri, payload, refresh_token=refresh_token)
    except TypeError:
        return method(uri, payload)


def response_success(value: Any) -> bool:
    """Accept only explicit Mijia success markers; never treat None as success."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in {"ok", "success"}
    if isinstance(value, (list, tuple)):
        return bool(value) and all(response_success(item) for item in value)
    if not isinstance(value, dict):
        return False

    if "code" in value:
        try:
            if int(value["code"]) != 0:
                return False
        except (TypeError, ValueError):
            return False
        if "result" not in value:
            return True
    if "result" in value:
        result = value["result"]
        if result in ({}, []):
            return "code" in value and int(value["code"]) == 0
        return response_success(result)
    return False


def response_error(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("message", "msg", "error", "description"):
            if value.get(key):
                return str(value[key])
        if "code" in value:
            return f"mijia_code_{value['code']}"
    return "mijia_response_not_success"


@contextmanager
def device_lock(did: str, timeout_sec: float) -> Iterator[None]:
    lock_path = Path("/tmp") / f"single_function_mijia_{did}.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout_sec
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MijiaError(f"device_busy:{did}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def find_device(api: Any, did: str) -> dict[str, Any] | None:
    getter = getattr(api, "get_devices_list", None)
    if not callable(getter):
        raise MijiaError("mijia_get_devices_list_unavailable")
    devices = list(getter() or [])
    shared_getter = getattr(api, "get_shared_devices_list", None)
    if callable(shared_getter):
        devices.extend(shared_getter() or [])
    for device in devices:
        if isinstance(device, dict) and str(device.get("did", "")) == str(did):
            return device
    return None


def safe_device_summary(device: dict[str, Any] | None, did: str) -> dict[str, Any]:
    if device is None:
        return {"did": did, "configured": True, "found": False, "online": None}
    return {
        "did": did,
        "configured": True,
        "found": True,
        "name": device.get("name", ""),
        "model": device.get("model", ""),
        "online": device.get("isOnline", device.get("is_online")),
    }
