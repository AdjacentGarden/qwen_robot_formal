#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
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


def clean_location_candidate(value: str) -> str | None:
    value = re.sub(r"^(?:请问|麻烦|帮我|帮忙|查一下|查询|看看|我想知道|告诉我)", "", value.strip())
    value = re.sub(r"[\s，,。？！!?：:；;、]+", "", value)
    temporal_or_local = r"(?:今天|今日|现在|当前|此刻|当地|这里|附近|本地)"
    value = re.sub(rf"^(?:{temporal_or_local})+", "", value)
    value = re.sub(rf"(?:{temporal_or_local}|的)+$", "", value).strip("的在")
    # ASR may drop the first character of a temporal word (for example,
    # "今天的天气" -> "天的天气"). A one-character prefix is not a reliable
    # place name and must not override the configured default location.
    if len(value) < 2 or value in {"我", "我们", "机器人"} or len(value) > 24:
        return None
    return value


def location_from_query(query: str, action: str) -> str | None:
    patterns = {
        "weather": [r"(.{2,24}?)(?:今天|现在|当前)?(?:的)?(?:天气|气温|温度|会不会下雨)"],
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


def weather(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    location = resolve_location(client, args)
    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 1,
        "timezone": "auto",
    }
    ttl = float(client.config.get("cache_ttl_seconds", {}).get("weather", 300))
    data, cached = client.json_request(client.config["endpoints"]["open_meteo_forecast"], params, cache_ttl=ttl)
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    code = int(current.get("weather_code", -1))
    description = WEATHER_CODES.get(code, "天气状况未知")
    temperature = current.get("temperature_2m")
    high = (daily.get("temperature_2m_max") or [None])[0]
    low = (daily.get("temperature_2m_min") or [None])[0]
    rain = (daily.get("precipitation_probability_max") or [None])[0]
    prefix = location_prefix(location)
    details = [f"当前{description}"]
    if temperature is not None:
        details.append(f"气温{temperature}摄氏度")
    if high is not None:
        details.append(f"今天最高{high}度")
    if low is not None:
        details.append(f"最低{low}度")
    if rain is not None:
        details.append(f"最大降雨概率{rain}%")
    message = prefix + "，".join(details) + "。"
    return result("weather", message, {"location": location, "current": current, "daily": daily}, "open_meteo", str(data.get("timezone") or config_timezone(client.config)), cached)


def location_query(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    location = resolve_location(client, args)
    if location.get("configured"):
        message = f"当前位置设为{location['name']}。这里使用的是区域中心坐标，不代表机器人的卫星定位精确位置。"
    elif location.get("source") == "ip_geolocation":
        message = f"网络粗略定位显示，机器人当前大约在{location['name']}。这个结果不是GPS精确位置。"
    elif location.get("approximate"):
        message = f"查询地点是{location['name']}。使用的是地点中心坐标。"
    else:
        message = f"当前查询位置是{location['name']}。"
    return result("location", message, location, str(location["source"]), str(location.get("timezone") or config_timezone(client.config)), bool(location.get("cache")))


def nearby_filter(query: str) -> tuple[str, str]:
    if re.search(r"吃|餐厅|饭店|美食|咖啡", query):
        return 'nwr(around:{radius},{lat},{lon})["amenity"~"restaurant|cafe|fast_food"];', "餐饮"
    if re.search(r"医院|诊所|药店|医疗", query):
        return 'nwr(around:{radius},{lat},{lon})["amenity"~"hospital|clinic|pharmacy"];', "医疗"
    if re.search(r"商场|购物|超市", query):
        return '(nwr(around:{radius},{lat},{lon})["shop"];nwr(around:{radius},{lat},{lon})["amenity"="marketplace"];);', "购物"
    return '(nwr(around:{radius},{lat},{lon})["tourism"];nwr(around:{radius},{lat},{lon})["leisure"~"park|garden|playground|sports_centre"];nwr(around:{radius},{lat},{lon})["amenity"~"cinema|theatre|arts_centre"];);', "休闲娱乐"


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


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query live time, weather, location, nearby places, or traffic.")
    parser.add_argument("--action", required=True, choices=("current_time", "weather", "location", "nearby", "traffic"))
    parser.add_argument("--query", default="")
    parser.add_argument("--location")
    parser.add_argument("--latitude", type=float)
    parser.add_argument("--longitude", type=float)
    parser.add_argument("--coordinate-system", choices=sorted(SUPPORTED_COORDINATE_SYSTEMS), default="wgs84")
    parser.add_argument("--radius", type=int)
    parser.add_argument("--limit", type=int)
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
        "nearby": lambda: nearby(client, args),
        "traffic": lambda: traffic(client, args),
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
