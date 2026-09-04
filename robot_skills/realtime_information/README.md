# Realtime information configuration

`config.json` contains the shared default location for time, weather, nearby-place, location, and traffic queries.

Location resolution order:

1. Coordinates or a place explicitly supplied in the current request.
2. A place extracted from the current user query.
3. The `location` object in `config.json`.
4. IP geolocation, only when `allow_ip_fallback` is `true`.

The default home address is `请配置家庭地址`; its Baidu door-address geocode is stored as WGS-84 `0.0, 0.0`. The saved company address is `请配置公司地址`; its verified door-address geocode is stored as WGS-84 `0.0, 0.0`. Unqualified local weather, nearby-place, and traffic queries use the home address. Queries that explicitly name the company use the saved company address. The skill converts WGS-84 to GCJ-02 before calling Baidu map APIs.

When changing `address`, update `latitude`, `longitude`, `coordinate_system`, `timezone`, and `precision` together. If coordinates are omitted, the skill attempts online place-name geocoding; configured coordinates are preferred because they remain deterministic when geocoding services are unavailable.

`precision: "address_geocoded"` records that the coordinates came from address geocoding. They are fixed configuration values, not a fresh satellite reading made during each query. Ordinary external-location questions get a short place-name answer; coordinates and the fixed-position boundary are spoken only when the user explicitly asks for coordinates, longitude, or latitude.

Generic questions such as “你在哪里” use indoor map localization. Only explicit GPS, coordinates, city, district, street, or other external-geography questions use this configured external position.

The skill uses a final-summary-only speech policy. It does not speak a generic start acknowledgement such as `收到`; it speaks the queried result once after execution completes.
