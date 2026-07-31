# Configuration & Storage Specification

The Rotor Virtual Limit Switch maintains persistent settings across service restarts using standard JSON files stored in the root working directory.

---

## 1. Primary Configuration File (`virtual_limits.json`)

The primary configuration file `virtual_limits.json` is created automatically on first run and updated in real-time as settings are modified via the Web UI or REST API.

### JSON Schema & Field Reference

```json
{
  "az_min": 45.0,
  "az_max": 315.0,
  "el_min": 0.0,
  "el_max": 85.0,
  "enabled": true,
  "last_az": 180.0,
  "last_el": 45.0,
  "backend_host": "127.0.0.1",
  "backend_port": 4533,
  "proxy_port": 4534,
  "commands_blocked": false,
  "refresh_interval_ms": 1000,
  "az_offset": 2.5,
  "el_offset": -1.0,
  "offset_enabled": true,
  "park_az": 180.0,
  "park_el": 0.0,
  "latitude": 37.7749,
  "longitude": -25.5197,
  "altitude": 100.0,
  "cable_guard": {
    "enabled": true,
    "max_turns": 1.5,
    "net_rotation": 180.0,
    "reference_az": 180.0
  },
  "tracking_sources": [
    {
      "url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle",
      "enabled": true
    },
    {
      "url": "https://celestrak.org/NORAD/elements/amateur.txt",
      "enabled": true
    }
  ]
}
```

### Field Descriptions

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `az_min` | `float` / `null` | `null` | Minimum permitted Azimuth boundary (degrees `0` to `360`). |
| `az_max` | `float` / `null` | `null` | Maximum permitted Azimuth boundary (degrees `0` to `360`). |
| `el_min` | `float` / `null` | `null` | Minimum permitted Elevation boundary (degrees `0` to `90`). |
| `el_max` | `float` / `null` | `null` | Maximum permitted Elevation boundary (degrees `0` to `90`). |
| `enabled` | `boolean` | `true` | Enables enforcement of virtual limit boundaries. |
| `last_az` | `float` / `null` | `null` | Last known physical rotor Azimuth position. |
| `last_el` | `float` / `null` | `null` | Last known physical rotor Elevation position. |
| `backend_host` | `string` | `"127.0.0.1"` | Upstream `rotctld` daemon IP address. |
| `backend_port` | `integer` | `4533` | Upstream `rotctld` daemon TCP port. |
| `proxy_port` | `integer` | `4534` | Proxy TCP listen port for client connections. |
| `commands_blocked` | `boolean` | `false` | When `true`, remote TCP client commands are rejected (Kill-Switch active). |
| `refresh_interval_ms` | `integer` | `1000` | Web UI status polling interval in milliseconds. |
| `az_offset` | `float` | `0.0` | Software Azimuth calibration offset (degrees). |
| `el_offset` | `float` | `0.0` | Software Elevation calibration offset (degrees). |
| `offset_enabled` | `boolean` | `true` | Master switch for calibration offset application. |
| `park_az` | `float` / `null` | `null` | Custom home/park target Azimuth position. |
| `park_el` | `float` / `null` | `null` | Custom home/park target Elevation position. |
| `latitude` | `float` / `null` | `null` | Ground station latitude in decimal degrees (e.g. `37.7749`). |
| `longitude` | `float` / `null` | `null` | Ground station longitude in decimal degrees (e.g. `-25.5197`). |
| `altitude` | `float` / `null` | `null` | Ground station height above sea level (meters MSL). |
| `cable_guard.enabled` | `boolean` | `false` | Enables multi-turn cable tangling guard. |
| `cable_guard.max_turns` | `float` | `1.0` | Maximum net rotation allowed (in full turns, e.g. `1.5`). |
| `cable_guard.net_rotation` | `float` | `0.0` | Accumulated net rotation degrees. |
| `tracking_sources` | `array` | Celestrak URLs | List of TLE download URLs for satellite orbital updates. |

---

## 2. Profile Management (`profiles/*.json`)

Named profiles allow saving and switching between operational presets (e.g., "Portable Ops", "Home Base", "Low-Profile Satellite").

* **Location:** Stored as individual JSON files in the `profiles/` directory (e.g., `profiles/portable-ops.json`).
* **Saved Parameters:** Includes limit bounds, cable guard states, ground station location, park coordinates, calibration offsets, and backend settings.

---

## 3. Orbital TLE Data Cache (`tle_cache.json`)

Downloaded Celestrak TLE data is cached locally to enable fast satellite search and offline pass predictions.

* **Location:** `tle_cache.json` in root directory.
* **Format:** Maps satellite names and NORAD IDs to TLE line pairs (`line1`, `line2`) with download timestamps.
* **Behavior:** Refreshed on demand or automatically when fetching satellite updates via the Web UI or `/api/tracking/fetch`.
