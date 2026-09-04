#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CACHE_DIR = ROOT / "runtime" / "cache"
USER_AGENT = "robot-realtime-information/1.1"
SUPPORTED_COORDINATE_SYSTEMS = {"wgs84", "gcj02"}
BAIDU_AK_PATH = Path(
    os.getenv("BAIDU_MAP_AK_FILE", str(ROOT / "runtime" / "baidu_map_ak"))
)

WEATHER_CODES = {
    0: "晴",
    1: "大致晴朗",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "有雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴冰雹",
}


class QueryError(RuntimeError):
    pass


def load_baidu_map_ak() -> str:
    """Load the Baidu Web Service AK without ever storing it in source/config."""

    value = os.environ.get("BAIDU_MAP_AK", "").strip()
    if value:
        return value
    try:
        mode = stat.S_IMODE(BAIDU_AK_PATH.stat().st_mode)
        if mode & 0o077:
            raise QueryError(f"baidu_ak_permissions_too_open:{oct(mode)}")
        return BAIDU_AK_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise QueryError(f"baidu_ak_read_failed:{type(exc).__name__}") from exc


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    validate_config(config)
    return config


def config_timezone(config: dict[str, Any]) -> str:
    profile = config.get("location") if isinstance(config.get("location"), dict) else {}
    return str(profile.get("timezone") or config.get("timezone") or "Asia/Shanghai")


def validate_config(config: dict[str, Any]) -> None:
    profile = config.get("location")
    if profile is not None and not isinstance(profile, dict):
        raise QueryError("invalid_config:location_must_be_object")
    profile = profile or {}
    timezone = config_timezone(config)
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise QueryError(f"invalid_config:timezone:{timezone}") from exc
    latitude = profile.get("latitude")
    longitude = profile.get("longitude")
    if (latitude is None) != (longitude is None):
        raise QueryError("invalid_config:latitude_and_longitude_must_be_set_together")
    if latitude is not None:
        validate_coordinates(float(latitude), float(longitude))
    system = str(profile.get("coordinate_system") or "wgs84").lower()
    if system not in SUPPORTED_COORDINATE_SYSTEMS:
        raise QueryError(f"invalid_config:coordinate_system:{system}")


class Client:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.timeout = float(config.get("request_timeout_seconds", 12))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cache_path(key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return CACHE_DIR / f"{digest}.json"

    def _read_cache(self, key: str, ttl: float) -> Any | None:
        path = self._cache_path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(payload["saved_at"]) <= ttl:
                return payload["value"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return None

    def _write_cache(self, key: str, value: Any) -> None:
        path = self._cache_path(key)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"saved_at": time.time(), "value": value}, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def json_request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        data: bytes | None = None,
        cache_key: str | None = None,
        cache_ttl: float = 0,
    ) -> tuple[Any, bool]:
        if params:
            separator = "&" if "?" in url else "?"
            url = url + separator + urllib.parse.urlencode(params)
        key = cache_key or url
        if cache_ttl > 0:
            cached = self._read_cache(key, cache_ttl)
            if cached is not None:
                return cached, True
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise QueryError(f"network_request_failed:{type(exc).__name__}:{exc}") from exc
        if cache_ttl > 0:
            self._write_cache(key, value)
        return value, False


def now_iso(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")


def validate_coordinates(latitude: float, longitude: float) -> None:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise QueryError(f"invalid_coordinates:{latitude},{longitude}")


def _out_of_china(longitude: float, latitude: float) -> bool:
    return not (72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271)


def _transform_latitude(longitude: float, latitude: float) -> float:
    value = -100.0 + 2.0 * longitude + 3.0 * latitude + 0.2 * latitude**2
    value += 0.1 * longitude * latitude + 0.2 * math.sqrt(abs(longitude))
    value += (20.0 * math.sin(6.0 * longitude * math.pi) + 20.0 * math.sin(2.0 * longitude * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(latitude * math.pi) + 40.0 * math.sin(latitude / 3.0 * math.pi)) * 2.0 / 3.0
    value += (160.0 * math.sin(latitude / 12.0 * math.pi) + 320.0 * math.sin(latitude * math.pi / 30.0)) * 2.0 / 3.0
    return value


def _transform_longitude(longitude: float, latitude: float) -> float:
    value = 300.0 + longitude + 2.0 * latitude + 0.1 * longitude**2
    value += 0.1 * longitude * latitude + 0.1 * math.sqrt(abs(longitude))
    value += (20.0 * math.sin(6.0 * longitude * math.pi) + 20.0 * math.sin(2.0 * longitude * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(longitude * math.pi) + 40.0 * math.sin(longitude / 3.0 * math.pi)) * 2.0 / 3.0
    value += (150.0 * math.sin(longitude / 12.0 * math.pi) + 300.0 * math.sin(longitude / 30.0 * math.pi)) * 2.0 / 3.0
    return value


def wgs84_to_gcj02(longitude: float, latitude: float) -> tuple[float, float]:
    if _out_of_china(longitude, latitude):
        return longitude, latitude
    semi_major_axis = 6378245.0
    eccentricity = 0.006693421622965943
    delta_latitude = _transform_latitude(longitude - 105.0, latitude - 35.0)
    delta_longitude = _transform_longitude(longitude - 105.0, latitude - 35.0)
    radian_latitude = latitude / 180.0 * math.pi
    magic = 1 - eccentricity * math.sin(radian_latitude) ** 2
    sqrt_magic = math.sqrt(magic)
    delta_latitude = delta_latitude * 180.0 / ((semi_major_axis * (1 - eccentricity)) / (magic * sqrt_magic) * math.pi)
    delta_longitude = delta_longitude * 180.0 / (semi_major_axis / sqrt_magic * math.cos(radian_latitude) * math.pi)
    return longitude + delta_longitude, latitude + delta_latitude


def gcj02_to_wgs84(longitude: float, latitude: float) -> tuple[float, float]:
    converted_longitude, converted_latitude = wgs84_to_gcj02(longitude, latitude)
    return longitude * 2 - converted_longitude, latitude * 2 - converted_latitude


def coordinate_location(
    latitude: float,
    longitude: float,
    *,
    name: str,
    source: str,
    coordinate_system: str = "wgs84",
    timezone: str | None = None,
    configured: bool = False,
    precision: str = "coordinates",
) -> dict[str, Any]:
    validate_coordinates(latitude, longitude)
    system = str(coordinate_system or "wgs84").lower()
    if system not in SUPPORTED_COORDINATE_SYSTEMS:
        raise QueryError(f"unsupported_coordinate_system:{system}")
    input_latitude, input_longitude = latitude, longitude
    if system == "gcj02":
        longitude, latitude = gcj02_to_wgs84(longitude, latitude)
    return {
        "latitude": latitude,
        "longitude": longitude,
        "name": name,
        "timezone": timezone,
        "source": source,
        "configured": configured,
        "approximate": precision not in {"exact", "gps"},
        "precision": precision,
        "coordinate_system": "wgs84",
        "input_coordinates": {
            "latitude": input_latitude,
            "longitude": input_longitude,
            "coordinate_system": system,
        },
        "cache": False,
    }


def amap_coordinates(location: dict[str, Any]) -> tuple[float, float]:
    source = location.get("input_coordinates") or {}
    if str(source.get("coordinate_system") or "").lower() == "gcj02":
        return float(source["longitude"]), float(source["latitude"])
    return wgs84_to_gcj02(float(location["longitude"]), float(location["latitude"]))


def baidu_gcj_coordinates(location: dict[str, Any]) -> tuple[float, float]:
    """Return (longitude, latitude) in GCJ-02 for Baidu Web APIs."""

    return amap_coordinates(location)


EXTERNAL_LOCATION_PATTERN = re.compile(
    r"GPS|GNSS|卫星定位|经纬度|坐标|外部位置|外面位置|地理位置|"
    r"(?:哪个|哪座|什么|所在(?:的)?)(?:国家|省份?|城市|市|区县|行政区|街道|道路|镇|乡|村)|"
    r"哪条(?:街|道路|路)|"
    r"行政区|地图上(?:的)?位置",
    re.IGNORECASE,
)

EXTERNAL_COORDINATE_DETAIL_PATTERN = re.compile(
    r"经纬度|经度|纬度|(?:GPS|GNSS).*(?:坐标|数值|多少)|"
    r"(?:具体|精确|详细)(?:的)?坐标|坐标(?:是|为|多少|数值)|多少度",
    re.IGNORECASE,
)
EXTERNAL_CITY_PATTERN = re.compile(r"哪个城市|哪座城市|所在(?:的)?城市|在哪个市|当前城市")
EXTERNAL_STREET_PATTERN = re.compile(r"哪个街道|哪条街|哪条路|所在(?:的)?街道|当前街道")


def location_action_for_query(query: str) -> str:
    """Default robot-location questions to indoor map localization."""

    return "external_location" if EXTERNAL_LOCATION_PATTERN.search(str(query or "")) else "indoor_location"


def _room_distance(room: dict[str, Any], x: float, y: float) -> float:
    anchor = room.get("anchor") if isinstance(room.get("anchor"), dict) else {}
    return math.hypot(x - float(anchor.get("x", 0.0)), y - float(anchor.get("y", 0.0)))


def classify_indoor_room(config: dict[str, Any], x: float, y: float) -> dict[str, Any]:
    indoor = config.get("indoor_location") if isinstance(config.get("indoor_location"), dict) else {}
    rooms = [item for item in indoor.get("rooms", []) if isinstance(item, dict)]
    if not rooms:
        return {"name": "unknown", "display_name": "家中未标注区域", "confidence": 0.0, "distance_meters": None}
    ranked = sorted(((_room_distance(room, x, y), room) for room in rooms), key=lambda item: item[0])
    distance, room = ranked[0]
    radius = float(room.get("radius_meters") or indoor.get("default_room_radius_meters") or 2.0)
    if distance > radius:
        return {
            "name": "unknown",
            "display_name": "家中未标注区域",
            "confidence": 0.0,
            "distance_meters": round(distance, 3),
            "nearest_room": str(room.get("name") or "unknown"),
            "nearest_display_name": str(room.get("display_name") or room.get("name") or "未知区域"),
        }
    confidence = max(0.0, min(1.0, 1.0 - distance / max(radius, 0.001)))
    return {
        "name": str(room.get("name") or "unknown"),
        "display_name": str(room.get("display_name") or room.get("name") or "未知区域"),
        "confidence": round(confidence, 3),
        "distance_meters": round(distance, 3),
    }


def read_indoor_map_pose(config: dict[str, Any]) -> dict[str, Any]:
    """Read map->base transform without publishing commands or touching hardware."""

    indoor = config.get("indoor_location") if isinstance(config.get("indoor_location"), dict) else {}
    map_frame = str(indoor.get("map_frame") or "map")
    base_frames = [str(item) for item in indoor.get("base_frames", ["base_footprint", "base_link"])]
    timeout = max(0.2, float(indoor.get("lookup_timeout_seconds") or 1.5))
    try:
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node
        from rclpy.signals import SignalHandlerOptions
        from rclpy.time import Time
        from tf2_ros import Buffer, TransformListener
    except Exception as exc:
        return {"available": False, "error": f"tf_import_failed:{type(exc).__name__}"}

    context = Context()
    executor = node = listener = None
    try:
        rclpy.init(args=None, context=context, signal_handler_options=SignalHandlerOptions.NO)
        node = Node(f"indoor_location_reader_{os.getpid()}_{time.monotonic_ns() % 1000000}", context=context)
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        buffer = Buffer(node=node)
        listener = TransformListener(buffer, node, spin_thread=False)
        deadline = time.monotonic() + timeout
        transform = None
        selected_base = None
        last_error = "transform_unavailable"
        while context.ok() and time.monotonic() < deadline and transform is None:
            executor.spin_once(timeout_sec=0.05)
            for base_frame in base_frames:
                try:
                    transform = buffer.lookup_transform(map_frame, base_frame, Time())
                    selected_base = base_frame
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}:{exc}"
        if transform is None:
            return {"available": False, "error": f"map_transform_unavailable:{last_error}"}
        translation = transform.transform.translation
        rotation = transform.transform.rotation
        yaw = math.atan2(
            2.0 * (rotation.w * rotation.z + rotation.x * rotation.y),
            1.0 - 2.0 * (rotation.y * rotation.y + rotation.z * rotation.z),
        )
        stamp = transform.header.stamp
        stamp_seconds = float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0
        now_seconds = node.get_clock().now().nanoseconds / 1_000_000_000.0
        age = max(0.0, now_seconds - stamp_seconds) if stamp_seconds > 0 else None
        max_age = float(indoor.get("max_transform_age_seconds") or 2.0)
        if age is not None and age > max_age:
            return {"available": False, "error": "map_transform_stale", "age_seconds": round(age, 3)}
        return {
            "available": True,
            "frame_id": map_frame,
            "base_frame": selected_base,
            "x": float(translation.x),
            "y": float(translation.y),
            "yaw": yaw,
            "stamp_seconds": stamp_seconds,
            "age_seconds": round(age, 3) if age is not None else None,
        }
    except Exception as exc:
        return {"available": False, "error": f"tf_lookup_failed:{type(exc).__name__}:{exc}"}
    finally:
        if executor is not None and node is not None:
            with contextlib.suppress(Exception):
                executor.remove_node(node)
        if node is not None:
            with contextlib.suppress(Exception):
                node.destroy_node()
        if context.ok():
            with contextlib.suppress(Exception):
                rclpy.shutdown(context=context)


def clean_location_candidate(value: str) -> str | None:
    value = re.sub(r"^(?:请问|麻烦|帮我|帮忙|查一下|查询|看看|我想知道|告诉我)", "", value.strip())
    value = re.sub(r"[\s，,。？！!?：:；;、]+", "", value)
    temporal_or_local = r"(?:今天|今日|明天|明日|后天|现在|当前|此刻|当地|这里|附近|本地)"
    value = re.sub(rf"^(?:{temporal_or_local})+", "", value)
    value = re.sub(rf"(?:{temporal_or_local}|的)+$", "", value).strip("的在")
    # ASR may drop the first character of a temporal word (for example,
    # "今天的天气" -> "天的天气"). A one-character prefix is not a reliable
    # place name and must not override the configured default location.
    if len(value) < 2 or value in {"我", "我们", "机器人"} or len(value) > 24:
        return None
    # A conversational prefix before “天气” is not a location.  Baidu may
    # otherwise accept phrases such as “我等会儿要出门了” as a low-confidence
    # NoClass geocode and return an unrelated city.  Keep clean place names,
    # saved-place aliases and normal address suffixes available.
    saved_aliases = {
        "家", "家庭", "家里", "住宅", "住处", "家庭位置", "家里的位置",
        "公司", "单位", "上班地点", "工作地点", "公司地址", "理想公司",
        "理想汽车", "理想汽车研发总部", "研发总部",
    }
    if value in saved_aliases:
        return value
    non_location_terms = (
        "我", "我们", "你", "机器人", "等会", "一会", "待会", "出门", "外出",
        "穿什么", "穿哪", "衣服", "穿搭", "适合", "合适", "觉得", "应该",
        "要不要", "需不需要", "带伞", "外面", "这边", "这里", "当地",
    )
    if any(term in value for term in non_location_terms):
        return None
    return value


def location_from_query(query: str, action: str) -> str | None:
    patterns = {
        "weather": [
            r"(.{2,24}?)(?:今天|今日|明天|明日|后天|现在|当前)?(?:的)?(?:天气|气温|温度|会不会下雨)",
            r"(.{2,24}?)(?:今天|今日|明天|明日|后天)?(?:出门|外出)?(?:应该|该|适合|比较适合|要)?(?:穿什么|穿哪(?:件|套)?|怎么穿|穿衣|穿搭)",
        ],
        "nearby": [r"(.{2,24}?)(?:附近|周边)(?:有|的|哪里|哪儿)"],
        "traffic": [r"(.{2,24}?)(?:今天|今日|现在|当前|此刻)?(?:的)?(?:交通|路况|堵车|拥堵|堵不堵|堵吗|车多不多)"],
        "current_time": [r"(.{2,24}?)(?:现在|当前)?(?:几点|时间|日期|几号|星期几)"],
    }
    for pattern in patterns.get(action, []):
        match = re.search(pattern, query)
        if match:
            candidate = clean_location_candidate(match.group(1))
            if candidate:
                return candidate
    return None


def geocode(client: Client, name: str) -> dict[str, Any]:
    baidu_key = load_baidu_map_ak()
    if baidu_key:
        ttl = float(client.config.get("cache_ttl_seconds", {}).get("geocoding", 86400))
        data, cached = client.json_request(
            client.config["endpoints"]["baidu_geocoding"],
            {"address": name, "output": "json", "ret_coordtype": "gcj02ll", "ak": baidu_key},
            cache_ttl=ttl,
        )
        if int(data.get("status", -1)) == 0 and isinstance(data.get("result"), dict):
            item = data["result"]
            point = item.get("location") or {}
            confidence = int(item.get("confidence") or 0)
            level = str(item.get("level") or "").strip().lower()
            # `status == 0` alone does not mean the address was understood.
            # Baidu returns plausible coordinates for some arbitrary phrases
            # with confidence=50 and level=NoClass.  Never treat those as a
            # user-specified location; fall through to the secondary resolver.
            reliable = level != "noclass" and confidence >= 60
            if reliable and point.get("lat") is not None and point.get("lng") is not None:
                location = coordinate_location(
                    float(point["lat"]),
                    float(point["lng"]),
                    name=name,
                    source="baidu_geocoding",
                    coordinate_system="gcj02",
                    timezone=config_timezone(client.config),
                    precision="named_place_center",
                )
                location.update(
                    {
                        "configured": False,
                        "approximate": True,
                        "cache": cached,
                        "confidence": item.get("confidence"),
                        "level": item.get("level"),
                    }
                )
                return location
    endpoint = client.config["endpoints"]["open_meteo_geocoding"]
    data, cached = client.json_request(endpoint, {"name": name, "count": 1, "language": "zh", "format": "json"}, cache_ttl=86400)
    results = data.get("results") or []
    if not results:
        raise QueryError(f"location_not_found:{name}")
    item = results[0]
    display = "".join(filter(None, [str(item.get("country") or ""), str(item.get("admin1") or ""), str(item.get("name") or "")]))
    return {
        "latitude": float(item["latitude"]),
        "longitude": float(item["longitude"]),
        "name": display or name,
        "timezone": item.get("timezone"),
        "source": "open_meteo_geocoding",
        "configured": False,
        "approximate": True,
        "precision": "named_place_center",
        "coordinate_system": "wgs84",
        "cache": cached,
    }


def baidu_suggest_place(client: Client, name: str) -> dict[str, Any]:
    """Resolve a POI name with local-city context while allowing other cities.

    Address geocoding is a poor fit for names such as ``红花湖`` and can pick
    a distant same-name address. Baidu's place suggestion endpoint ranks POIs
    using the configured city as context, but ``city_limit=false`` still lets
    an explicit destination such as ``深圳北站`` resolve outside Huizhou.
    """

    baidu_key = load_baidu_map_ak()
    if not baidu_key:
        raise QueryError("baidu_map_ak_not_configured")
    endpoint = str((client.config.get("endpoints") or {}).get("baidu_place_suggestion") or "")
    if not endpoint:
        raise QueryError("baidu_place_suggestion_not_configured")
    profile = client.config.get("location") if isinstance(client.config.get("location"), dict) else {}
    region = str(profile.get("city") or profile.get("district") or "全国").strip()
    data, cached = client.json_request(
        endpoint,
        {
            "query": name,
            "region": region,
            "city_limit": "false",
            "output": "json",
            "ret_coordtype": "gcj02ll",
            "ak": baidu_key,
        },
        cache_ttl=float(client.config.get("cache_ttl_seconds", {}).get("geocoding", 86400)),
    )
    if int(data.get("status", -1)) != 0:
        raise QueryError(f"baidu_place_suggestion_error:{data.get('status')}:{data.get('message') or 'unknown'}")
    usable = [
        item for item in (data.get("result") or [])
        if isinstance(item, dict)
        and isinstance(item.get("location"), dict)
        and item["location"].get("lat") is not None
        and item["location"].get("lng") is not None
    ]
    if not usable:
        raise QueryError(f"location_not_found:{name}")
    normalized = re.sub(r"[\s，,。？！!?:：；;、]", "", name)
    best = next(
        (
            item for item in usable
            if re.sub(r"[\s，,。？！!?:：；;、]", "", str(item.get("name") or "")) == normalized
        ),
        usable[0],
    )
    point = best["location"]
    location = coordinate_location(
        float(point["lat"]),
        float(point["lng"]),
        name=name,
        source="baidu_place_suggestion",
        coordinate_system="gcj02",
        timezone=config_timezone(client.config),
        precision="poi_center",
    )
    location.update(
        {
            "configured": False,
            "approximate": True,
            "cache": cached,
            "resolved_name": str(best.get("name") or name),
            "city": best.get("city"),
            "district": best.get("district"),
            "uid": best.get("uid"),
        }
    )
    return location


def ip_location(client: Client) -> dict[str, Any]:
    ttl = float(client.config.get("cache_ttl_seconds", {}).get("ip_location", 3600))
    data, cached = client.json_request(client.config["endpoints"]["ip_location"], cache_ttl=ttl)
    if not data.get("success", False):
        raise QueryError(f"ip_location_failed:{data.get('message', 'unknown')}")
    city = str(data.get("city") or "")
    region = str(data.get("region") or "")
    country = str(data.get("country") or "")
    display_name = "，".join(filter(None, [country, region, city]))
    if city:
        try:
            display_name = geocode(client, city)["name"]
        except QueryError:
            pass
    return {
        "latitude": float(data["latitude"]),
        "longitude": float(data["longitude"]),
        "name": display_name,
        "city": city,
        "region": region,
        "country": country,
        "timezone": (data.get("timezone") or {}).get("id"),
        "source": "ip_geolocation",
        "configured": False,
        "approximate": True,
        "precision": "ip_geolocation",
        "coordinate_system": "wgs84",
        "cache": cached,
    }


def configured_location(client: Client) -> dict[str, Any] | None:
    profile = client.config.get("location")
    if not isinstance(profile, dict):
        return None
    address = str(profile.get("address") or "").strip()
    latitude = profile.get("latitude")
    longitude = profile.get("longitude")
    if latitude is not None and longitude is not None:
        return coordinate_location(
            float(latitude),
            float(longitude),
            name=address or "配置位置",
            source="configured_coordinates",
            coordinate_system=str(profile.get("coordinate_system") or "wgs84"),
            timezone=str(profile.get("timezone") or config_timezone(client.config)),
            configured=True,
            precision=str(profile.get("precision") or "configured_coordinates"),
        )
    if address:
        location = geocode(client, address)
        location.update(
            {
                "name": address,
                "source": "configured_address_geocoding",
                "configured": True,
                "approximate": True,
                "precision": "geocoded_place_center",
                "timezone": profile.get("timezone") or location.get("timezone") or config_timezone(client.config),
            }
        )
        return location
    return None


def saved_place_location(client: Client, name: str | None) -> dict[str, Any] | None:
    """Resolve an explicitly named saved place without sending its alias to geocoding."""

    normalized = re.sub(r"[\s，,。？！!?:：；;、]", "", str(name or ""))
    normalized = re.sub(r"(?:附近|周边|那边|那里|那儿)$", "", normalized)
    if not normalized:
        return None
    profiles = client.config.get("saved_places")
    if not isinstance(profiles, dict):
        return None
    for key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        display_name = str(profile.get("display_name") or key)
        aliases = [str(key), display_name, *[str(value) for value in profile.get("aliases", [])]]
        if normalized not in {re.sub(r"\s", "", value) for value in aliases if value}:
            continue
        latitude = profile.get("latitude")
        longitude = profile.get("longitude")
        if latitude is None or longitude is None:
            raise QueryError(f"saved_place_coordinates_missing:{key}")
        location = coordinate_location(
            float(latitude),
            float(longitude),
            name=display_name,
            source="saved_place",
            coordinate_system=str(profile.get("coordinate_system") or "wgs84"),
            timezone=str(profile.get("timezone") or config_timezone(client.config)),
            configured=True,
            precision=str(profile.get("precision") or "configured_coordinates"),
        )
        location["saved_place"] = str(key)
        if profile.get("district_id"):
            location["district_id"] = str(profile["district_id"])
        if profile.get("address"):
            location["address"] = str(profile["address"])
        return location
    return None


def resolve_location(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    latitude = getattr(args, "latitude", None)
    longitude = getattr(args, "longitude", None)
    if (latitude is None) != (longitude is None):
        raise QueryError("latitude_and_longitude_must_be_set_together")
    if latitude is not None:
        return coordinate_location(
            float(latitude),
            float(longitude),
            name=getattr(args, "location", None) or "指定坐标",
            source="explicit_coordinates",
            coordinate_system=str(getattr(args, "coordinate_system", None) or "wgs84"),
            timezone=config_timezone(client.config),
            precision="exact",
        )
    query = str(getattr(args, "query", None) or "")
    action = str(getattr(args, "action", None) or "")
    name = getattr(args, "location", None) or location_from_query(query, action)
    if name:
        saved = saved_place_location(client, str(name))
        if saved is not None:
            return saved
        return geocode(client, str(name))
    configured = configured_location(client)
    if configured is not None:
        return configured
    legacy_location = client.config.get("default_location")
    if legacy_location:
        location = geocode(client, str(legacy_location))
        location.update({"configured": True, "source": "legacy_default_location"})
        return location
    profile = client.config.get("location") if isinstance(client.config.get("location"), dict) else {}
    if profile.get("allow_ip_fallback", True) is False:
        raise QueryError("location_not_configured_and_ip_fallback_disabled")
    return ip_location(client)


def result(action: str, message: str, payload: Any, source: str, timezone: str, cached: bool = False, **extra: Any) -> dict[str, Any]:
    value = {
        "ok": True,
        "status": "completed",
        "skill": "realtime_information",
        "action": action,
        "message": message,
        "result": payload,
        "source": source,
        "fetched_at": now_iso(timezone),
        "cache": cached,
        "error": None,
    }
    value.update(extra)
    return value


def location_prefix(location: dict[str, Any]) -> str:
    if location.get("configured"):
        return f"当前位置{location['name']}，"
    if location.get("source") == "ip_geolocation":
        return f"根据网络定位，当前位置大约是{location['name']}，"
    if location.get("approximate"):
        return f"查询位置{location['name']}，"
    return f"当前位置{location['name']}，"


def current_time(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    location = resolve_location(client, args)
    timezone = str(location.get("timezone") or config_timezone(client.config))
    current = datetime.now(ZoneInfo(timezone))
    weekdays = "一二三四五六日"
    prefix = f"当前位置{location['name']}，" if location.get("configured") else f"{location['name']}当地，"
    message = f"{prefix}现在是{current.year}年{current.month}月{current.day}日，星期{weekdays[current.weekday()]}，{current.hour}点{current.minute:02d}分{current.second:02d}秒。"
    return result("current_time", message, {"datetime": current.isoformat(), "timezone": timezone, "location": location}, "system_clock", timezone)


def baidu_district_id(client: Client, location: dict[str, Any], key: str) -> str | None:
    if location.get("district_id"):
        return str(location["district_id"])
    profile = client.config.get("location") if isinstance(client.config.get("location"), dict) else {}
    if location.get("source") == "configured_coordinates" and profile.get("district_id"):
        return str(profile["district_id"])
    longitude, latitude = baidu_gcj_coordinates(location)
    data, _ = client.json_request(
        client.config["endpoints"]["baidu_reverse_geocoding"],
        {
            "location": f"{latitude},{longitude}",
            "coordtype": "gcj02ll",
            "ret_coordtype": "gcj02ll",
            "output": "json",
            "ak": key,
        },
        cache_ttl=float(client.config.get("cache_ttl_seconds", {}).get("geocoding", 86400)),
    )
    if int(data.get("status", -1)) != 0:
        return None
    component = ((data.get("result") or {}).get("addressComponent") or {})
    value = component.get("adcode")
    return str(value) if value else None


_CLOTHING_ADVICE_PATTERN = re.compile(
    r"穿什么|穿哪(?:件|套)?|怎么穿|穿衣|衣服|穿搭|搭配|带什么外套"
)


def requested_weather_day(query: str) -> tuple[int, str]:
    """Return the forecast index and natural-language label requested by the user."""

    text = str(query or "")
    if "后天" in text:
        return 2, "后天"
    if "明天" in text or "明日" in text:
        return 1, "明天"
    return 0, "今天"


def wants_clothing_advice(query: str) -> bool:
    """Keep the user's practical goal after the weather lookup is complete."""

    return bool(_CLOTHING_ADVICE_PATTERN.search(str(query or "")))


def _as_temperature(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def clothing_advice(
    *,
    high: Any,
    low: Any,
    weather_text: str = "",
    current_temperature: Any = None,
) -> str:
    """Build a concise, data-grounded clothing suggestion for speech output."""

    high_value = _as_temperature(high)
    low_value = _as_temperature(low)
    current_value = _as_temperature(current_temperature)
    available = [value for value in (high_value, low_value, current_value) if value is not None]
    warmest = max(available) if available else None
    coolest = min(available) if available else None

    if warmest is None:
        advice = "建议按体感分层穿，出门前再确认一下室外温度"
    elif warmest <= 5 or (coolest is not None and coolest < 0):
        advice = "建议穿保暖内层和羽绒服，怕冷的话再加围巾"
    elif warmest <= 12 or (coolest is not None and coolest <= 5):
        advice = "建议穿毛衣配厚外套，早晚注意保暖"
    elif warmest <= 20 or (coolest is not None and coolest <= 12):
        advice = "建议穿长袖配外套，温度升高时可以脱掉外层"
    elif warmest <= 27:
        advice = "建议穿薄长袖或短袖配一件轻薄外套"
    elif coolest is not None and coolest < 20:
        advice = "白天适合短袖，早晚最好带一件轻薄外套"
    else:
        advice = "建议穿透气的短袖和轻薄下装"

    conditions = str(weather_text or "")
    extras: list[str] = []
    if re.search(r"雨|雷|阵雨|冰雹", conditions):
        extras.append("记得带伞")
    if re.search(r"雪|雨夹雪", conditions):
        extras.append("鞋子尽量选防滑防水的")
    if re.search(r"晴", conditions) and warmest is not None and warmest >= 26:
        extras.append("注意防晒")
    if extras:
        advice += "，" + "，".join(extras)
    return advice + "。"


def _forecast_value(values: Any, index: int) -> Any:
    return values[index] if isinstance(values, list) and len(values) > index else None


def weather(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    location = resolve_location(client, args)
    query = str(getattr(args, "query", "") or "")
    day_offset, day_label = requested_weather_day(query)
    include_clothing_advice = wants_clothing_advice(query)
    baidu_key = load_baidu_map_ak()
    if baidu_key:
        district_id = baidu_district_id(client, location, baidu_key)
        if district_id:
            ttl = float(client.config.get("cache_ttl_seconds", {}).get("weather", 300))
            data, cached = client.json_request(
                client.config["endpoints"]["baidu_weather"],
                {"district_id": district_id, "data_type": "all", "ak": baidu_key},
                cache_ttl=ttl,
            )
            if int(data.get("status", -1)) == 0 and isinstance(data.get("result"), dict):
                value = data["result"]
                now = value.get("now") or {}
                forecasts = value.get("forecasts") or []
                selected = forecasts[day_offset] if len(forecasts) > day_offset else {}
                place = value.get("location") or {}
                display = "".join(filter(None, [str(place.get("city") or ""), str(place.get("name") or "")])) or location["name"]
                forecast_text = str(selected.get("text_day") or selected.get("text_night") or "天气状况未知")
                if day_offset == 0:
                    details = [f"当前{now.get('text') or forecast_text}"]
                    if now.get("temp") is not None:
                        details.append(f"气温{now['temp']}摄氏度")
                    if selected.get("high") is not None:
                        details.append(f"今天最高{selected['high']}度")
                    if selected.get("low") is not None:
                        details.append(f"最低{selected['low']}度")
                    if now.get("rh") is not None:
                        details.append(f"湿度{now['rh']}%")
                else:
                    details = [f"{day_label}{forecast_text}"]
                    if selected.get("high") is not None:
                        details.append(f"最高{selected['high']}度")
                    if selected.get("low") is not None:
                        details.append(f"最低{selected['low']}度")
                message = f"{display}，" + "，".join(details) + "。"
                if include_clothing_advice:
                    message += clothing_advice(
                        high=selected.get("high"),
                        low=selected.get("low"),
                        weather_text=" ".join(
                            filter(None, [forecast_text, str(selected.get("text_night") or "")])
                        ),
                        current_temperature=now.get("temp") if day_offset == 0 else None,
                    )
                return result(
                    "weather",
                    message,
                    {
                        "location": location,
                        "district_id": district_id,
                        "now": now,
                        "forecasts": forecasts,
                        "requested_day_offset": day_offset,
                        "requested_day_label": day_label,
                        "selected_forecast": selected,
                        "clothing_advice_requested": include_clothing_advice,
                    },
                    "baidu_weather",
                    config_timezone(client.config),
                    cached,
                )
    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": max(1, day_offset + 1),
        "timezone": "auto",
    }
    ttl = float(client.config.get("cache_ttl_seconds", {}).get("weather", 300))
    data, cached = client.json_request(client.config["endpoints"]["open_meteo_forecast"], params, cache_ttl=ttl)
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    daily_code = _forecast_value(daily.get("weather_code"), day_offset)
    code = int(current.get("weather_code", -1) if day_offset == 0 else (daily_code if daily_code is not None else -1))
    description = WEATHER_CODES.get(code, "天气状况未知")
    temperature = current.get("temperature_2m") if day_offset == 0 else None
    high = _forecast_value(daily.get("temperature_2m_max"), day_offset)
    low = _forecast_value(daily.get("temperature_2m_min"), day_offset)
    rain = _forecast_value(daily.get("precipitation_probability_max"), day_offset)
    prefix = location_prefix(location)
    details = [f"当前{description}" if day_offset == 0 else f"{day_label}{description}"]
    if temperature is not None:
        details.append(f"气温{temperature}摄氏度")
    if high is not None:
        details.append(f"{day_label}最高{high}度" if day_offset == 0 else f"最高{high}度")
    if low is not None:
        details.append(f"最低{low}度")
    if rain is not None:
        details.append(f"最大降雨概率{rain}%")
    message = prefix + "，".join(details) + "。"
    if include_clothing_advice:
        message += clothing_advice(
            high=high,
            low=low,
            weather_text=description,
            current_temperature=temperature,
        )
    return result(
        "weather",
        message,
        {
            "location": location,
            "current": current,
            "daily": daily,
            "requested_day_offset": day_offset,
            "requested_day_label": day_label,
            "clothing_advice_requested": include_clothing_advice,
        },
        "open_meteo",
        str(data.get("timezone") or config_timezone(client.config)),
        cached,
    )


def external_location_query(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    location = resolve_location(client, args)
    query = str(getattr(args, "query", "") or "")
    detailed_coordinates = bool(EXTERNAL_COORDINATE_DETAIL_PATTERN.search(query))
    if location.get("configured") and location.get("precision") == "gps":
        if detailed_coordinates:
            message = (
                f"按配置的GPS坐标，机器人外部位置是{location['name']}，"
                f"北纬{float(location['latitude']):.6f}度，东经{float(location['longitude']):.6f}度。"
                "这是固定配置位置，不是本次实时卫星测量。"
            )
        elif EXTERNAL_CITY_PATTERN.search(query):
            city = str((client.config.get("location") or {}).get("city") or location["name"])
            message = f"我当前在{city}。"
        elif EXTERNAL_STREET_PATTERN.search(query):
            message = f"我当前在{location['name']}。"
        else:
            message = f"按当前位置设置，我在{location['name']}。"
    elif location.get("configured"):
        message = f"当前位置设为{location['name']}。这里使用的是区域中心坐标，不代表机器人的卫星定位精确位置。"
    elif location.get("source") == "ip_geolocation":
        message = f"网络粗略定位显示，机器人当前大约在{location['name']}。这个结果不是GPS精确位置。"
    elif location.get("approximate"):
        message = f"查询地点是{location['name']}。使用的是地点中心坐标。"
    else:
        message = f"当前查询位置是{location['name']}。"
    return result("external_location", message, location, str(location["source"]), str(location.get("timezone") or config_timezone(client.config)), bool(location.get("cache")))


def indoor_location_query(
    client: Client,
    args: argparse.Namespace,
    pose_provider=read_indoor_map_pose,
) -> dict[str, Any]:
    pose = pose_provider(client.config)
    timezone = config_timezone(client.config)
    if not pose.get("available"):
        message = "我现在读不到室内地图定位，所以暂时无法确定是在客厅、书房还是餐厅。"
        return result(
            "indoor_location",
            message,
            {"available": False, "pose": pose, "room": None},
            "map_tf_unavailable",
            timezone,
            available=False,
        )
    room = classify_indoor_room(client.config, float(pose["x"]), float(pose["y"]))
    if room["name"] == "unknown":
        nearest = room.get("nearest_display_name")
        suffix = f"，离{nearest}最近" if nearest else ""
        message = f"我目前在家里的未标注区域{suffix}。"
    else:
        message = f"我现在在{room['display_name']}。"
    return result(
        "indoor_location",
        message,
        {"available": True, "pose": pose, "room": room},
        "map_tf",
        timezone,
        available=True,
    )


def location_query(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    """Backward-compatible location action with indoor-first semantics."""

    action = location_action_for_query(str(getattr(args, "query", "") or ""))
    if action == "external_location":
        return external_location_query(client, args)
    return indoor_location_query(client, args)


def nearby_filter(query: str) -> tuple[str, str]:
    if re.search(r"吃|餐厅|饭店|美食|咖啡", query):
        return 'nwr(around:{radius},{lat},{lon})["amenity"~"restaurant|cafe|fast_food"];', "餐饮"
    if re.search(r"医院|诊所|药店|医疗", query):
        return 'nwr(around:{radius},{lat},{lon})["amenity"~"hospital|clinic|pharmacy"];', "医疗"
    if re.search(r"商场|购物|超市", query):
        return '(nwr(around:{radius},{lat},{lon})["shop"];nwr(around:{radius},{lat},{lon})["amenity"="marketplace"];);', "购物"
    return '(nwr(around:{radius},{lat},{lon})["tourism"];nwr(around:{radius},{lat},{lon})["leisure"~"park|garden|playground|sports_centre"];nwr(around:{radius},{lat},{lon})["amenity"~"cinema|theatre|arts_centre"];);', "休闲娱乐"


def baidu_place_query(query: str) -> tuple[str, str]:
    if re.search(r"吃|餐厅|饭店|美食|咖啡", query):
        return "餐厅", "餐饮"
    if re.search(r"医院|诊所|药店|医疗", query):
        return "医院", "医疗"
    if re.search(r"商场|购物|超市", query):
        return "商场", "购物"
    if re.search(r"公园", query):
        return "公园", "公园"
    if re.search(r"电影院|电影", query):
        return "电影院", "电影院"
    return "休闲娱乐", "休闲娱乐"


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value)))


def nearby(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    location = resolve_location(client, args)
    radius = int(args.radius or client.config.get("nearby_radius_meters", 2000))
    limit = int(args.limit or client.config.get("nearby_limit", 5))
    baidu_key = load_baidu_map_ak()
    if baidu_key:
        keyword, category = baidu_place_query(args.query or "")
        longitude, latitude = baidu_gcj_coordinates(location)
        ttl = float(client.config.get("cache_ttl_seconds", {}).get("nearby", 600))
        data, cached = client.json_request(
            client.config["endpoints"]["baidu_place_search"],
            {
                "query": keyword,
                "location": f"{latitude},{longitude}",
                "radius": min(max(radius, 100), 50000),
                "output": "json",
                "scope": 2,
                "page_size": min(max(limit, 1), 20),
                "coord_type": 2,
                "ret_coordtype": "gcj02ll",
                "ak": baidu_key,
            },
            cache_ttl=ttl,
        )
        if int(data.get("status", -1)) == 0:
            places = []
            for item in (data.get("results") or [])[:limit]:
                point = item.get("location") or {}
                distance = (item.get("detail_info") or {}).get("distance")
                if distance is None and point.get("lat") is not None and point.get("lng") is not None:
                    distance = haversine(latitude, longitude, float(point["lat"]), float(point["lng"]))
                places.append(
                    {
                        "name": str(item.get("name") or "未命名地点"),
                        "distance_meters": int(float(distance)) if distance is not None else None,
                        "address": item.get("address"),
                        "uid": item.get("uid"),
                    }
                )
            if places:
                names = "、".join(
                    f"{item['name']}，约{item['distance_meters']}米" if item.get("distance_meters") is not None else item["name"]
                    for item in places
                )
                message = f"{location_prefix(location)}百度地图中附近的{category}地点有：{names}。"
            else:
                message = f"{location_prefix(location)}在{radius}米范围内暂时没有查到合适的{category}地点。"
            return result(
                "nearby",
                message,
                {"location": location, "category": category, "radius_meters": radius, "places": places},
                "baidu_place",
                str(location.get("timezone") or config_timezone(client.config)),
                cached,
            )
    clause, category = nearby_filter(args.query or "")
    clause = clause.format(radius=radius, lat=location["latitude"], lon=location["longitude"])
    overpass_query = f"[out:json][timeout:12];{clause}out center tags 50;"
    ttl = float(client.config.get("cache_ttl_seconds", {}).get("nearby", 600))
    data, cached = client.json_request(
        client.config["endpoints"]["overpass"],
        data=urllib.parse.urlencode({"data": overpass_query}).encode("utf-8"),
        cache_key=f"overpass:{overpass_query}",
        cache_ttl=ttl,
    )
    places: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data.get("elements") or []:
        tags = item.get("tags") or {}
        name = str(tags.get("name:zh") or tags.get("name") or "").strip()
        center = item.get("center") or item
        if not name or name in seen or center.get("lat") is None or center.get("lon") is None:
            continue
        seen.add(name)
        distance = haversine(location["latitude"], location["longitude"], float(center["lat"]), float(center["lon"]))
        places.append({"name": name, "distance_meters": distance, "type": tags.get("tourism") or tags.get("leisure") or tags.get("amenity") or tags.get("shop")})
    places.sort(key=lambda item: item["distance_meters"])
    places = places[:limit]
    prefix = location_prefix(location)
    if places:
        names = "、".join(f"{item['name']}，约{item['distance_meters']}米" for item in places)
        message = f"{prefix}地图数据中附近的{category}地点有：{names}。"
    else:
        message = f"{prefix}在{radius}米范围的地图数据中暂时没有查到合适的{category}地点。"
    return result("nearby", message, {"location": location, "category": category, "radius_meters": radius, "places": places}, "openstreetmap_overpass", str(location.get("timezone") or config_timezone(client.config)), cached)


def traffic(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    baidu_key = load_baidu_map_ak()
    if baidu_key:
        query = str(args.query or "")
        road_match = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]{2,24}(?:高速|大道|公路|街|路))", query)
        road_name = road_match.group(1) if road_match else None
        if road_name and re.search(r"当前|现在|附近|周边|实时|交通|路况|道路", road_name):
            road_name = None
        location_args = args
        if road_name and not getattr(args, "location", None) and getattr(args, "latitude", None) is None:
            location_args = argparse.Namespace(**vars(args))
            location_args.query = ""
        location = resolve_location(client, location_args)
        radius = min(max(int(args.radius or 1500), 200), 5000)
        longitude, latitude = baidu_gcj_coordinates(location)
        ttl = float(client.config.get("cache_ttl_seconds", {}).get("traffic", 120))
        if road_name:
            profile = client.config.get("location") if isinstance(client.config.get("location"), dict) else {}
            params = {
                "road_name": road_name,
                "city": str(profile.get("city") or "北京市"),
                "coord_type_output": "gcj02",
                "ak": baidu_key,
            }
            endpoint = client.config["endpoints"]["baidu_traffic_road"]
            mode = "road"
        else:
            params = {
                "center": f"{latitude},{longitude}",
                "radius": radius,
                "coord_type_input": "gcj02",
                "coord_type_output": "gcj02",
                "ak": baidu_key,
            }
            endpoint = client.config["endpoints"]["baidu_traffic_around"]
            mode = "around"
        data, cached = client.json_request(endpoint, params, cache_ttl=ttl)
        if int(data.get("status", -1)) != 0:
            raise QueryError(f"baidu_traffic_provider_error:{data.get('message') or data.get('msg') or 'unknown'}")
        evaluation = data.get("evaluation") or {}
        description = evaluation.get("status_desc") or "暂无概况"
        subject = road_name if road_name else f"{location['name']}附近"
        message = f"{subject}当前路况：{description}。"
        return result(
            "traffic",
            message,
            {
                "available": True,
                "location": location,
                "mode": mode,
                "evaluation": evaluation,
                "road_traffic": data.get("road_traffic") or [],
            },
            "baidu_traffic",
            str(location.get("timezone") or config_timezone(client.config)),
            cached,
            available=True,
        )
    key = os.environ.get("AMAP_WEB_SERVICE_KEY", "").strip()
    if not key:
        location = configured_location(client)
        prefix = f"当前位置已设为{location['name']}，但" if location else ""
        message = f"{prefix}实时交通数据服务还没有配置，因此我现在不能可靠地判断道路是否拥堵。"
        return result("traffic", message, {"available": False, "location": location}, "unconfigured", config_timezone(client.config), available=False)
    location = resolve_location(client, args)
    radius = min(int(args.radius or 1500), 5000)
    amap_longitude, amap_latitude = amap_coordinates(location)
    params = {"location": f"{amap_longitude},{amap_latitude}", "radius": radius, "key": key, "extensions": "base"}
    ttl = float(client.config.get("cache_ttl_seconds", {}).get("traffic", 120))
    data, cached = client.json_request(client.config["endpoints"]["amap_traffic"], params, cache_ttl=ttl)
    if str(data.get("status")) != "1":
        raise QueryError(f"traffic_provider_error:{data.get('info', 'unknown')}")
    evaluation = ((data.get("trafficinfo") or {}).get("evaluation") or {})
    description = evaluation.get("description") or "暂无概况"
    message = f"{location['name']}附近当前路况：{description}。"
    return result("traffic", message, {"available": True, "location": location, "evaluation": evaluation}, "amap_traffic", str(location.get("timezone") or config_timezone(client.config)), cached, available=True)


def _directionlite_route(
    client: Client,
    *,
    mode: str,
    origin: dict[str, Any],
    destination: dict[str, Any],
    baidu_key: str,
) -> tuple[dict[str, Any], bool]:
    endpoint_key = f"baidu_direction_{mode}"
    endpoint = str((client.config.get("endpoints") or {}).get(endpoint_key) or "")
    if not endpoint:
        raise QueryError(f"route_endpoint_not_configured:{mode}")
    params: dict[str, Any] = {
        "origin": f"{float(origin['latitude']):.6f},{float(origin['longitude']):.6f}",
        "destination": f"{float(destination['latitude']):.6f},{float(destination['longitude']):.6f}",
        "coord_type": "wgs84",
        "ak": baidu_key,
    }
    if mode == "driving":
        params["tactics"] = 2  # 躲避拥堵
    elif mode == "transit":
        params["tactics_incity"] = 5  # 地铁优先
    data, cached = client.json_request(
        endpoint,
        params,
        cache_ttl=float(client.config.get("cache_ttl_seconds", {}).get("route", 60)),
    )
    if int(data.get("status", -1)) != 0:
        raise QueryError(f"baidu_{mode}_route_error:{data.get('status')}:{data.get('message') or 'unknown'}")
    routes = ((data.get("result") or {}).get("routes") or [])
    usable = [
        route for route in routes
        if isinstance(route, dict) and route.get("duration") is not None and route.get("distance") is not None
    ]
    if not usable:
        raise QueryError(f"baidu_{mode}_route_empty")
    best = min(usable, key=lambda route: float(route["duration"]))
    route_summary = {
        "mode": mode,
        "route_count": len(usable),
        "distance_meters": int(float(best["distance"])),
        "duration_seconds": int(float(best["duration"])),
        "traffic_condition": best.get("traffic_condition"),
        "toll_yuan": best.get("toll"),
    }
    if mode == "transit":
        itinerary: list[dict[str, Any]] = []
        raw_legs = best.get("steps") or []
        for raw_leg in raw_legs:
            steps = raw_leg if isinstance(raw_leg, list) else [raw_leg]
            for step in steps:
                if not isinstance(step, dict):
                    continue
                vehicle = step.get("vehicle") if isinstance(step.get("vehicle"), dict) else {}
                instruction = re.sub(r"<[^>]+>", "", str(step.get("instruction") or "")).strip()
                item = {
                    "type": "walk" if int(step.get("type") or -1) == 5 else "transit",
                    "instruction": instruction,
                    "distance_meters": int(float(step.get("distance") or 0)),
                    "duration_seconds": int(float(step.get("duration") or 0)),
                }
                if vehicle.get("name"):
                    item.update(
                        {
                            "line": str(vehicle.get("name") or "").strip(),
                            "direction": str(vehicle.get("direct_text") or "").strip(),
                            "board_at": str(vehicle.get("start_name") or "").strip(),
                            "leave_at": str(vehicle.get("end_name") or "").strip(),
                            "stop_count": int(vehicle.get("stop_num") or 0),
                        }
                    )
                itinerary.append(item)
        route_summary["itinerary"] = itinerary
    return route_summary, cached


def _transit_itinerary_text(transit: dict[str, Any] | None) -> str:
    """Turn Baidu's best transit route into a short, speakable transfer guide."""

    if not transit:
        return ""
    parts: list[str] = []
    for item in transit.get("itinerary") or []:
        if not isinstance(item, dict):
            continue
        line = str(item.get("line") or "").strip()
        if line:
            board_at = str(item.get("board_at") or "").strip()
            leave_at = str(item.get("leave_at") or "").strip()
            direction = str(item.get("direction") or "").strip()
            stop_count = int(item.get("stop_count") or 0)
            segment = f"在{board_at}乘{line}" if board_at else f"乘{line}"
            if direction:
                segment += f"往{direction}"
            if leave_at:
                segment += f"，到{leave_at}下车"
            if stop_count:
                segment += f"，共{stop_count}站"
            parts.append(segment)
        elif item.get("type") == "walk":
            distance = int(item.get("distance_meters") or 0)
            if distance >= 80:
                parts.append(f"步行约{distance}米")
    return "，再".join(parts[:6])


def commute_recommendation(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    """Compare live driving and metro-priority routes to an external place."""

    baidu_key = load_baidu_map_ak()
    if not baidu_key:
        raise QueryError("baidu_map_ak_not_configured")
    origin_name = str(getattr(args, "origin", None) or "家庭").strip()
    destination_name = str(getattr(args, "destination", None) or "").strip()
    if not destination_name:
        raise QueryError("commute_destination_missing")

    def resolve_route_place(name: str) -> dict[str, Any]:
        if name in {"当前位置", "这里", "此处", "我这里"}:
            configured = configured_location(client)
            if configured is None:
                raise QueryError("commute_current_location_unavailable")
            return configured
        saved = saved_place_location(client, name)
        if saved is not None:
            return saved
        try:
            return baidu_suggest_place(client, name)
        except QueryError:
            location = geocode(client, name)
            location["name"] = name
            return location

    origin = resolve_route_place(origin_name)
    destination = resolve_route_place(destination_name)
    route_errors: dict[str, str] = {}
    try:
        driving, driving_cached = _directionlite_route(
            client,
            mode="driving",
            origin=origin,
            destination=destination,
            baidu_key=baidu_key,
        )
    except QueryError as exc:
        driving, driving_cached = None, False
        route_errors["driving"] = str(exc)
    try:
        transit, transit_cached = _directionlite_route(
            client,
            mode="transit",
            origin=origin,
            destination=destination,
            baidu_key=baidu_key,
        )
    except QueryError as exc:
        transit, transit_cached = None, False
        route_errors["transit"] = str(exc)
    if driving is None and transit is None:
        raise QueryError("commute_routes_unavailable:" + json.dumps(route_errors, ensure_ascii=False))
    if driving is None:
        transit_minutes = max(1, round(transit["duration_seconds"] / 60))
        transit_km = transit["distance_meters"] / 1000.0
        message = (
            f"现在从{origin['name']}到{destination['name']}，地铁优先的公交方案预计约{transit_minutes}分钟、"
            f"{transit_km:.1f}公里。驾车路线这次没有查到，所以暂时更推荐公交地铁。"
        )
        recommendation, saved_minutes = "transit", None
    elif transit is None:
        driving_minutes = max(1, round(driving["duration_seconds"] / 60))
        driving_km = driving["distance_meters"] / 1000.0
        message = (
            f"现在从{origin['name']}到{destination['name']}，开车预计约{driving_minutes}分钟、{driving_km:.1f}公里。"
            "公交地铁这次没有查到可用方案，所以暂时更推荐开车。"
        )
        recommendation, saved_minutes = "driving", None
    else:
        driving_minutes = max(1, round(driving["duration_seconds"] / 60))
        transit_minutes = max(1, round(transit["duration_seconds"] / 60))
        driving_km = driving["distance_meters"] / 1000.0
        transit_km = transit["distance_meters"] / 1000.0
        traffic_text = {
            0: "暂无整体路况评价",
            1: "整体畅通",
            2: "整体缓行",
            3: "整体拥堵",
            4: "整体严重拥堵",
        }.get(int(driving.get("traffic_condition") or 0), "路况信息未知")
        saved_minutes = abs(transit_minutes - driving_minutes)
        if driving_minutes + 8 <= transit_minutes:
            recommendation = "driving"
            recommendation_zh = "更推荐开车"
            reason = f"预计可以节省约{saved_minutes}分钟"
        elif transit_minutes + 5 < driving_minutes:
            recommendation = "transit"
            recommendation_zh = "更推荐坐地铁或公交"
            reason = f"预计可以节省约{saved_minutes}分钟"
        else:
            recommendation = "either"
            recommendation_zh = "两种方式用时接近"
            reason = "可以再根据停车和换乘便利程度选择"
        message = (
            f"现在从{origin['name']}到{destination['name']}，开车预计约{driving_minutes}分钟、{driving_km:.1f}公里，{traffic_text}；"
            f"地铁优先的公交方案预计约{transit_minutes}分钟、{transit_km:.1f}公里。"
            f"按当前时间比较，{recommendation_zh}，{reason}。"
        )
    itinerary_text = _transit_itinerary_text(transit)
    if itinerary_text:
        message += f" 公交路线大致是：{itinerary_text}。"
    return result(
        "commute_recommendation",
        message,
        {
            "origin": {"name": origin["name"], "saved_place": origin.get("saved_place")},
            "destination": {"name": destination["name"], "saved_place": destination.get("saved_place")},
            "driving": driving,
            "transit": transit,
            "recommendation": recommendation,
            "saved_minutes": saved_minutes,
            "route_errors": route_errors,
        },
        "baidu_directionlite",
        str(origin.get("timezone") or config_timezone(client.config)),
        bool((driving is None or driving_cached) and (transit is None or transit_cached)),
        available=True,
    )


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query live time, weather, location, nearby places, or traffic.")
    parser.add_argument(
        "--action",
        required=True,
        choices=("current_time", "weather", "location", "indoor_location", "external_location", "nearby", "traffic", "commute_recommendation"),
    )
    parser.add_argument("--query", default="")
    parser.add_argument("--location")
    parser.add_argument("--latitude", type=float)
    parser.add_argument("--longitude", type=float)
    parser.add_argument("--coordinate-system", choices=sorted(SUPPORTED_COORDINATE_SYSTEMS), default="wgs84")
    parser.add_argument("--radius", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--origin")
    parser.add_argument("--destination")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config()
    if args.dry_run:
        print(json.dumps({"ok": True, "skill": "realtime_information", "action": args.action, "query": args.query, "dry_run": True}, ensure_ascii=False))
        return 0
    client = Client(config)
    handlers = {
        "current_time": lambda: current_time(client, args),
        "weather": lambda: weather(client, args),
        "location": lambda: location_query(client, args),
        "indoor_location": lambda: indoor_location_query(client, args),
        "external_location": lambda: external_location_query(client, args),
        "nearby": lambda: nearby(client, args),
        "traffic": lambda: traffic(client, args),
        "commute_recommendation": lambda: commute_recommendation(client, args),
    }
    try:
        payload = handlers[args.action]()
    except QueryError as exc:
        payload = {
            "ok": False,
            "status": "failed",
            "skill": "realtime_information",
            "action": args.action,
            "message": "实时信息查询暂时失败，请稍后再试。",
            "result": None,
            "source": None,
            "fetched_at": now_iso(config_timezone(config)),
            "error": str(exc),
        }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
