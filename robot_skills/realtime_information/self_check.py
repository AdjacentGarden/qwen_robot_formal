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
    classify_indoor_room,
    clothing_advice,
    commute_recommendation,
    config_timezone,
    current_time,
    external_location_query,
    indoor_location_query,
    location_action_for_query,
    location_from_query,
    resolve_location,
    requested_weather_day,
    traffic,
    validate_config,
    wants_clothing_advice,
)


def main() -> int:
    assert requested_weather_day("今天穿什么衣服") == (0, "今天")
    assert requested_weather_day("明天穿什么衣服") == (1, "明天")
    assert requested_weather_day("后天出门怎么穿") == (2, "后天")
    assert wants_clothing_advice("今天应该穿什么衣服出门") is True
    assert wants_clothing_advice("明天天气怎么样") is False
    warm_advice = clothing_advice(high=31, low=20, weather_text="晴", current_temperature=17)
    assert "短袖" in warm_advice and "轻薄外套" in warm_advice and "防晒" in warm_advice
    rainy_advice = clothing_advice(high=18, low=10, weather_text="中雨")
    assert "外套" in rainy_advice and "带伞" in rainy_advice
    cold_advice = clothing_advice(high=2, low=-5, weather_text="雪")
    assert "羽绒服" in cold_advice and "防滑防水" in cold_advice

    assert clean_location_candidate("今天，今天的") is None
    assert clean_location_candidate("天的") is None
    assert clean_location_candidate("今的") is None
    assert clean_location_candidate("深圳今天的") == "深圳"
    assert clean_location_candidate("上海附近") == "上海"

    assert location_from_query("深圳今天天气怎么样", "weather") == "深圳"
    assert location_from_query("今天天气怎么样", "weather") is None
    assert location_from_query("明天天气怎么样", "weather") is None
    assert location_from_query("后天天气怎么样", "weather") is None
    assert location_from_query("上海明天天气怎么样", "weather") == "上海"
    assert location_from_query("上海明天穿什么衣服合适", "weather") == "上海"
    assert location_from_query("我后天出门该怎么穿", "weather") is None
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
        "saved_places": {
            "home": {
                "display_name": "家庭",
                "aliases": ["家", "家庭", "家里"],
                "latitude": 0.0,
                "longitude": 0.0,
                "coordinate_system": "wgs84",
                "timezone": "Asia/Shanghai",
                "precision": "gps",
            },
            "company": {
                "display_name": "公司",
                "aliases": ["公司", "单位", "上班地点"],
                "latitude": 0.0,
                "longitude": 0.0,
                "coordinate_system": "wgs84",
                "timezone": "Asia/Shanghai",
                "precision": "gps",
            }
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

    company_args = SimpleNamespace(**{**vars(args), "location": "公司", "query": "公司附近路况", "action": "traffic"})
    company_location = resolve_location(client, company_args)
    assert company_location["name"] == "公司"
    assert company_location["source"] == "saved_place"
    assert abs(company_location["latitude"] - 0.0) < 1e-12
    assert abs(company_location["longitude"] - 0.0) < 1e-12
    home_args = SimpleNamespace(**{**vars(args), "location": "家", "query": "家附近路况", "action": "traffic"})
    home_location = resolve_location(client, home_args)
    assert home_location["name"] == "家庭"
    assert home_location["source"] == "saved_place"
    assert abs(home_location["latitude"] - 0.0) < 1e-12
    assert abs(home_location["longitude"] - 0.0) < 1e-12

    asr_fragment_args = SimpleNamespace(**{**vars(args), "query": "天的天气怎么样？", "action": "weather"})
    asr_fragment_location = resolve_location(client, asr_fragment_args)
    assert asr_fragment_location["configured"] is True
    assert asr_fragment_location["name"] == "北京市顺义区"

    time_args = SimpleNamespace(**{**vars(args), "query": "现在几点", "action": "current_time"})
    time_payload = current_time(client, time_args)
    assert time_payload["result"]["timezone"] == "Asia/Shanghai"
    assert "当前位置北京市顺义区" in time_payload["message"]

    assert location_action_for_query("你现在在哪里") == "indoor_location"
    assert location_action_for_query("你在客厅还是书房") == "indoor_location"
    assert location_action_for_query("你现在在哪个城市") == "external_location"
    assert location_action_for_query("GPS定位在哪里") == "external_location"
    room_config = {
        "indoor_location": {
            "rooms": [
                {"name": "living_room", "display_name": "客厅", "anchor": {"x": -2.2, "y": 0.1}, "radius_meters": 1.8}
            ]
        }
    }
    assert classify_indoor_room(room_config, -2.2, 0.1)["display_name"] == "客厅"
    indoor_payload = indoor_location_query(
        client,
        SimpleNamespace(**{**vars(args), "action": "indoor_location", "query": "你在哪里"}),
        pose_provider=lambda _config: {"available": False, "error": "self_check_no_tf"},
    )
    assert indoor_payload["source"] == "map_tf_unavailable"
    assert "北京市顺义区" not in indoor_payload["message"]

    gps_config = {
        "timezone": "Asia/Shanghai",
        "request_timeout_seconds": 1,
        "location": {
            "address": "请配置家庭地址",
            "latitude": 0.0,
            "longitude": 0.0,
            "coordinate_system": "wgs84",
            "timezone": "Asia/Shanghai",
            "precision": "gps",
            "allow_ip_fallback": False,
        },
    }
    gps_payload = external_location_query(
        Client(gps_config),
        SimpleNamespace(**{**vars(args), "action": "external_location", "query": "GPS定位在哪里"}),
    )
    assert gps_payload["source"] == "configured_coordinates"
    assert gps_payload["result"]["precision"] == "gps"
    assert gps_payload["result"]["coordinate_system"] == "wgs84"
    assert "北纬" not in gps_payload["message"]
    assert "按当前位置设置" in gps_payload["message"]

    detailed_gps_payload = external_location_query(
        Client(gps_config),
        SimpleNamespace(**{**vars(args), "action": "external_location", "query": "你的具体经纬度是多少"}),
    )
    assert "北纬39.995524度" in detailed_gps_payload["message"]
    assert "固定配置位置" in detailed_gps_payload["message"]
    assert "实时卫星测量" in detailed_gps_payload["message"]

    route_config = {
        "timezone": "Asia/Shanghai",
        "request_timeout_seconds": 1,
        "cache_ttl_seconds": {"route": 0},
        "location": gps_config["location"],
        "saved_places": {
            "home": {
                "display_name": "家庭", "aliases": ["家", "家庭"],
                "latitude": 0.0, "longitude": 0.0,
                "coordinate_system": "wgs84", "timezone": "Asia/Shanghai", "precision": "gps",
            },
            "company": {
                "display_name": "公司", "aliases": ["公司", "单位"],
                "latitude": 0.0, "longitude": 0.0,
                "coordinate_system": "wgs84", "timezone": "Asia/Shanghai", "precision": "gps",
            },
        },
        "endpoints": {
            "baidu_direction_driving": "https://example.invalid/driving",
            "baidu_direction_transit": "https://example.invalid/transit",
        },
    }
    route_client = Client(route_config)

    def fake_route_request(endpoint, params=None, cache_ttl=None):
        if endpoint.endswith("/driving"):
            return {"status": 0, "message": "ok", "result": {"routes": [{"distance": 14221, "duration": 1757, "traffic_condition": 1}]}}, False
        return {"status": 0, "message": "ok", "result": {"routes": [{"distance": 13979, "duration": 3620}]}}, False

    with patch("run.load_baidu_map_ak", return_value="self-check-key"), patch.object(
        route_client, "json_request", side_effect=fake_route_request
    ):
        commute_payload = commute_recommendation(
            route_client,
            SimpleNamespace(origin="家庭", destination="公司", query="去公司开车还是坐地铁"),
        )
    assert commute_payload["ok"] is True
    assert commute_payload["result"]["recommendation"] == "driving"
    assert commute_payload["result"]["origin"]["name"] == "家庭"
    assert commute_payload["result"]["destination"]["name"] == "公司"
    assert "更推荐开车" in commute_payload["message"]

    with patch("run.load_baidu_map_ak", return_value=""), patch.dict(os.environ, {"AMAP_WEB_SERVICE_KEY": ""}):
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
    with patch("run.load_baidu_map_ak", return_value=""), patch.dict(os.environ, {"AMAP_WEB_SERVICE_KEY": "test-key"}):
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
