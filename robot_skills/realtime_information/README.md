# Realtime information configuration

`config.json` contains the shared default location for time, weather, nearby-place, location, and traffic queries.

Location resolution order:

1. Coordinates or a place explicitly supplied in the current request.
2. A place extracted from the current user query.
3. The `location` object in `config.json`.
4. IP geolocation, only when `allow_ip_fallback` is `true`.

The configured coordinates for Beijing Shunyi District come from an Amap district-center result and are therefore marked `gcj02`. The skill converts them to WGS84 for Open-Meteo and OpenStreetMap, while Amap traffic requests use GCJ-02. Do not relabel a coordinate system without converting the numbers.

When changing `address`, update `latitude`, `longitude`, `coordinate_system`, `timezone`, and `precision` together. If coordinates are omitted, the skill attempts online place-name geocoding; configured coordinates are preferred because they remain deterministic when geocoding services are unavailable.

`precision: "district_center"` means the configured point represents the district center. It is not the robot's GPS position.

The skill uses a final-summary-only speech policy. It does not speak a generic start acknowledgement such as `收到`; it speaks the queried result once after execution completes.
