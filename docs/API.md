# REST API Reference

The Rotor Virtual Limit Switch server provides a comprehensive HTTP REST API on port `8080` (configurable via `--http-port`). All POST endpoints expect JSON payloads with `Content-Type: application/json` and return JSON responses.

---

## Response Schema

### Success Response
Most endpoints return a JSON object with `"ok": true`:
```json
{
  "ok": true
}
```

### Error Response
When a request fails or is rejected (e.g. by limit enforcement or missing parameters), the response returns `"ok": false` with an `"error"` message and optionally a Hamlib response code (`"response"`):
```json
{
  "ok": false,
  "error": "Azimuth 45.0° below minimum limit 90.0°",
  "response": "RPRT -15"
}
```

---

## 1. System Status

### `GET /api/status`
Returns the complete operational state of the proxy, limits, cable guard, calibration offsets, ground station location, satellite tracker, and backend connection.

**Request:** `GET /api/status`

**Response Example:**
```json
{
  "limits": {
    "az_min": 45.0,
    "az_max": 315.0,
    "el_min": 0.0,
    "el_max": 85.0,
    "enabled": true
  },
  "cable_guard": {
    "enabled": true,
    "max_turns": 1.5,
    "net_rotation": 180.0,
    "reference_az": 180.0
  },
  "position": {
    "az": 180.0,
    "el": 45.0
  },
  "offsets": {
    "az_offset": 2.5,
    "el_offset": -1.0,
    "enabled": true
  },
  "park": {
    "az": 0.0,
    "el": 0.0
  },
  "location": {
    "latitude": 37.7749,
    "longitude": -25.5197,
    "altitude": 100.0
  },
  "commands_blocked": false,
  "backend": {
    "host": "127.0.0.1",
    "port": 4533,
    "reachable": true
  },
  "refresh_interval_ms": 1000,
  "tracking": {
    "active": false,
    "satellite": null
  }
}
```

---

## 2. Virtual Limits

### `POST /api/limits/manual`
Explicitly sets numerical limit boundaries for Azimuth and Elevation.

**Payload:**
```json
{
  "az_min": 45.0,
  "az_max": 315.0,
  "el_min": 0.0,
  "el_max": 85.0
}
```

### `POST /api/limits/set-left`
Captures current physical rotor Azimuth position as `az_min`.

### `POST /api/limits/set-right`
Captures current physical rotor Azimuth position as `az_max`.

### `POST /api/limits/set-down`
Captures current physical rotor Elevation position as `el_min`.

### `POST /api/limits/set-up`
Captures current physical rotor Elevation position as `el_max`.

### `POST /api/limits/enable`
Enables enforcement of virtual limit boundaries.

### `POST /api/limits/disable`
Disables virtual limit enforcement (unconstrained movement).

### `POST /api/limits/clear`
Clears all saved virtual limits (`az_min`, `az_max`, `el_min`, `el_max` set to `null`).

---

## 3. Cable Tangling Guard

### `POST /api/cable-guard/enable`
Enables the multi-turn cable tangling guard.

### `POST /api/cable-guard/disable`
Disables the cable guard.

### `POST /api/cable-guard/reset`
Resets accumulated net rotation counter to 0.0°.

### `POST /api/cable-guard/max-turns`
Sets the maximum permitted net rotation threshold (in full turns, e.g. `1.0`, `1.5`, `2.0`).

**Payload:**
```json
{
  "turns": 1.5
}
```

---

## 4. Manual Rotor Movement & Park

### `POST /api/rotor/goto`
Commands rotor to specified Azimuth and Elevation coordinates.

**Payload:**
```json
{
  "az": 180.0,
  "el": 45.0
}
```

### `POST /api/rotor/move`
Sends step movement command. Direction flags: `2` = Up, `4` = Down, `8` = Left, `16` = Right.

**Payload:**
```json
{
  "direction": 16,
  "speed": 5.0
}
```

### `POST /api/rotor/stop`
Issues immediate emergency stop command (`S`).

### `POST /api/rotor/park`
Commands rotor to move to configured park position.

### `GET /api/rotor/park-position`
Retrieves saved park coordinates.

**Response:**
```json
{
  "ok": true,
  "az": 0.0,
  "el": 0.0
}
```

### `POST /api/rotor/park-position`
Saves custom park coordinates.

**Payload:**
```json
{
  "az": 180.0,
  "el": 0.0
}
```

---

## 5. Offsets & Calibration

### `POST /api/offset/set`
Configures software calibration offset values for Azimuth and Elevation.

**Payload:**
```json
{
  "az_offset": 2.5,
  "el_offset": -1.0
}
```

### `POST /api/offset/clear`
Resets calibration offsets to `0.0`.

### `POST /api/offset/enable`
Enables software calibration offset adjustments.

### `POST /api/offset/disable`
Disables calibration offsets.

---

## 6. Satellite Tracker

### `POST /api/tracking/fetch`
Triggers TLE update from Celestrak or configured custom URL sources.

**Payload:**
```json
{
  "force": true
}
```

### `POST /api/tracking/satellites`
Searches cached TLE satellites matching a search query string.

**Payload:**
```json
{
  "search": "ISS"
}
```

### `POST /api/tracking/passes`
Calculates upcoming satellite pass predictions, sky track, and ground track for the specified satellite by NORAD ID.

**Payload:**
```json
{
  "norad_id": 25544
}
```

### `POST /api/tracking/start`
Starts tracking engine for a satellite by NORAD ID.

**Payload:**
```json
{
  "norad_id": 25544
}
```

### `POST /api/tracking/stop`
Stops satellite tracking auto-steering.

### `GET /api/tracking/status`
Retrieves satellite tracking state. Append `?extra=1` for pass predictions and satellite ground track details.

**Request:** `GET /api/tracking/status?extra=1`

### `GET /api/tracking/sources`
Lists available TLE sources and cached satellite counts.

---

## 7. Command Blocking & Settings

### `POST /api/commands/block`
Blocks all external TCP commands from tracking software (Kill-Switch active).

### `POST /api/commands/unblock`
Restores normal TCP command proxying.

### `POST /api/refresh-interval`
Sets Web UI polling refresh interval in milliseconds (minimum `200` ms).

**Payload:**
```json
{
  "ms": 1000
}
```

### `GET /api/location`
Gets saved ground station location.

### `POST /api/location`
Saves ground station location parameters.

**Payload:**
```json
{
  "latitude": 37.7749,
  "longitude": -25.5197,
  "altitude": 100.0
}
```

---

## 8. Backend Configuration

### `POST /api/backend/config`
Updates upstream `rotctld` IP address and port.

**Payload:**
```json
{
  "host": "127.0.0.1",
  "port": 4533
}
```

### `POST /api/backend/reconnect`
Forces backend socket reconnection test.

### `POST /api/backend/test`
Tests TCP connection reachability and position query against upstream daemon.

---

## 9. Configuration Profiles

### `POST /api/profiles/list`
Lists saved configuration profiles and metadata.

**Response Example:**
```json
{
  "ok": true,
  "profiles": [
    {
      "name": "portable-station",
      "filename": "portable-station.json",
      "description": "Field operation limits and location",
      "created_at": "2026-08-01T12:00:00",
      "updated_at": "2026-08-02T10:30:00"
    }
  ]
}
```

### `POST /api/profiles/save`
Saves current configuration as a named profile preset.

**Payload:**
```json
{
  "name": "portable-station",
  "description": "Field operation limits and location"
}
```

### `POST /api/profiles/load`
Loads settings from a saved profile preset.

**Payload:**
```json
{
  "name": "portable-station"
}
```

### `POST /api/profiles/delete`
Deletes a saved profile preset.

**Payload:**
```json
{
  "name": "portable-station"
}
```
