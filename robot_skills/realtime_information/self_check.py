#!/usr/bin/env python3
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from run import (
    Client,
    QueryError,
    amap_coordinates,
    clean_location_candidate,
    config_timezone,
    current_time,
    location_from_query,
    resolve_location,
    traffic,
    validate_config,
)


def main() -> int:
    assert clean_location_candidate("今天，今天的") is None
    assert clean_location_candidate("天的") is None
    assert clean_location_candidate("今的") is None
    assert clean_location_candidate("深圳今天的") == "深圳"
    assert clean_location_candidate("上海附近") == "上海"

    assert location_from_query("深圳今天天气怎么样", "weather") == "深圳"
    assert location_from_query("今天天气怎么样", "weather") is None
    assert location_from_query("天的天气怎么样？", "weather") is None
    assert location_from_query("上海附近有什么好玩的", "nearby") == "上海"
    assert location_from_query("今天，今天的交通状况如何", "traffic") is None
    assert location_from_query("深圳现在堵不堵", "traffic") == "深圳"

    config = {
        "timezone": "UTC",
        "request_timeout_seconds": 1,
        "endpoints": {"amap_traffic": "https://example.invalid/amap-traffic"},
        "location": {
            "address": "北京市顺义区",
            "latitude": 40.149891,
            "longitude": 116.661474,
            "coordinate_system": "gcj02",
            "timezone": "Asia/Shanghai",
            "precision": "district_center",
            "allow_ip_fallback": False,
        },
    }
    validate_config(config)
    assert config_timezone(config) == "Asia/Shanghai"
    args = SimpleNamespace(
        query="今天，今天的交通状况如何",
        action="traffic",
        location=None,
        latitude=None,
        longitude=None,
        coordinate_system="wgs84",
        radius=None,
    )
    client = Client(config)
    location = resolve_location(client, args)
    assert location["configured"] is True
    assert location["name"] == "北京市顺义区"
    assert location["coordinate_system"] == "wgs84"
    assert abs(location["latitude"] - 40.148766) < 0.0001
    assert abs(location["longitude"] - 116.655546) < 0.0001
    amap_longitude, amap_latitude = amap_coordinates(location)
    assert abs(amap_latitude - 40.149891) < 0.000001
    assert abs(amap_longitude - 116.661474) < 0.000001

    asr_fragment_args = SimpleNamespace(**{**vars(args), "query": "天的天气怎么样？", "action": "weather"})
    asr_fragment_location = resolve_location(client, asr_fragment_args)
    assert asr_fragment_location["configured"] is True
    assert asr_fragment_location["name"] == "北京市顺义区"

    time_args = SimpleNamespace(**{**vars(args), "query": "现在几点", "action": "current_time"})
    time_payload = current_time(client, time_args)
    assert time_payload["result"]["timezone"] == "Asia/Shanghai"
    assert "按配置位置北京市顺义区" in time_payload["message"]

    with patch.dict(os.environ, {"AMAP_WEB_SERVICE_KEY": ""}):
        payload = traffic(client, args)
    assert payload["ok"] is True
    assert payload["available"] is False
    assert payload["result"]["location"]["name"] == "北京市顺义区"
    assert payload["source"] == "unconfigured"

    class FakeTrafficClient:
        def __init__(self, value: dict[str, object]):
            self.config = value
            self.params: dict[str, object] | None = None

        def json_request(self, url: str, params: dict[str, object], **_: object) -> tuple[dict[str, object], bool]:
            self.params = params
            return {"status": "1", "trafficinfo": {"evaluation": {"description": "畅通"}}}, False

    fake_client = FakeTrafficClient(config)
    with patch.dict(os.environ, {"AMAP_WEB_SERVICE_KEY": "test-key"}):
        traffic_payload = traffic(fake_client, args)  # type: ignore[arg-type]
    assert traffic_payload["ok"] is True
    assert fake_client.params is not None
    assert fake_client.params["location"] == "116.661474,40.149891"

    try:
        validate_config({"location": {"latitude": 22.5}})
    except QueryError:
        pass
    else:
        raise AssertionError("incomplete configured coordinates must fail validation")

    print("REALTIME_INFORMATION_SELF_CHECK_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
