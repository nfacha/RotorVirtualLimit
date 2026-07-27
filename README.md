# Rotor Virtual Limit Switch

A transparent TCP proxy that sits between Gpredict and `rotctld` (hamlib) to enforce virtual azimuth/elevation limits, a cable tangling guard, and command blocking — all configurable via a web UI. Designed for touch screens and mobile browsers.

```
Gpredict → TCP proxy (:4534) → rotctld (:4533)
               ↕
         Web UI (:8080)
```

## Quick start

```bash
# Clone the repo
git clone https://github.com/nfacha/RotorVirtualLimit.git
cd RotorVirtualLimit

# Start rotctld (example: Yaesu GS-232 on /dev/ttyUSB0)
rotctld -m 1 -r /dev/ttyUSB0 -s 9600 -C serial_speed=9600

# Start the proxy
python proxy.py

# Open the web UI
firefox http://localhost:8080
```

## Features

- **Virtual limit switches** — define safe az/el ranges; commands outside the range are rejected
- **Cable tangling guard** — tracks accumulated rotation and blocks further movement beyond a configurable number of turns
- **Command blocking** — toggle to reject all inbound TCP commands while keeping the rotor controllable from the web UI
- **Manual rotor control** — move/stop/goto/park from the browser, bypassing all limits
- **Park position** — save a target az/el and recall it with the Park button
- **Profiles** — save/load/delete named configurations including limits, cable guard, backend, park position, and location
- **Map view** — live Leaflet map showing station location and a real-time azimuth line (0° = North)
- **Backend status** — tri-state indicator (green/red/grey) for rotctld reachability
- **Configurable poll rate** — how often the UI refreshes position (default 1000ms)
- **Fully transparent** — passes through all hamlib commands not related to position/movement

## Requirements

- Python 3.12+ (stdlib only — no pip dependencies)
- A working `rotctld` instance (part of [hamlib](https://hamlib.github.io/))

## Command-line options

| Flag | Default | Description |
|------|---------|-------------|
| `--listen-port` | `4534` | Port the proxy listens on |
| `--backend` | `127.0.0.1:4533` | `rotctld` host and port |
| `--http-port` | `8080` | Web UI port |
| `--config` | `virtual_limits.json` | Path to config file |
| `--disabled` | — | Start with virtual limits disabled |
| `--cable-guard` | — | Start with cable guard enabled |

## Web UI

| Section | Description |
|---------|-------------|
| **Dashboard** | Current azimuth (horizontal bar) and elevation (vertical meter) |
| **Map** | Station location marker + real-time azimuth line |
| **Manual Control** | D-pad directions + step slider (°/press), goto (az/el), park |
| **Virtual Limits** | Toggle on/off, capture current position as az/el min/max, manual inputs, range bars |
| **Cable Tangling Guard** | Toggle on/off, max turns, net rotation bar, reset |
| **Backend** | Host/port configuration, test connectivity, reconnect |
| **Poll Rate** | Configurable status refresh interval (ms) |
| **Station Location** | Latitude/longitude (saved to profile, shown on map) |
| **Profiles** | Save, load, delete named configurations |

## API Reference

All API endpoints return JSON. POST endpoints accept `Content-Type: application/json`.

### Status

GET `/api/status`
Returns the full proxy state: positions, limits, cable guard, backend, park position, command block flag.

### Limits

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/limits/manual` | POST | Set az/el limits (`az_min`, `az_max`, `el_min`, `el_max`) |
| `/api/limits/set-left` | POST | Capture current az as min |
| `/api/limits/set-right` | POST | Capture current az as max |
| `/api/limits/set-down` | POST | Capture current el as min |
| `/api/limits/set-up` | POST | Capture current el as max |
| `/api/limits/enable` | POST | Enable virtual limits |
| `/api/limits/disable` | POST | Disable virtual limits |
| `/api/limits/clear` | POST | Clear all limits |

### Cable Guard

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cable-guard/enable` | POST | Enable cable guard |
| `/api/cable-guard/disable` | POST | Disable cable guard |
| `/api/cable-guard/reset` | POST | Reset net rotation counter |
| `/api/cable-guard/max-turns` | POST | Set max turns (`turns`) |

### Manual Rotor Control

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/rotor/goto` | POST | Move to position (`az`, `el`) |
| `/api/rotor/move` | POST | Move direction (`direction`: 2=up,4=down,8=left,16=right, `speed`: degrees per press, default 5) |
| `/api/rotor/stop` | POST | Stop movement |
| `/api/rotor/park` | POST | Go to saved park position, or send hamlib park command |
| `/api/rotor/park-position` | GET | Get saved park position |
| `/api/rotor/park-position` | POST | Set park position (`az`, `el`) |

### Backend

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/backend/config` | POST | Set backend host/port (`host`, `port`) |
| `/api/backend/reconnect` | POST | Force reconnection on next command |
| `/api/backend/test` | POST | Test connectivity (`host`, `port`) |

### Settings

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/refresh-interval` | POST | Set UI poll rate in ms (`ms`, min 200) |

### Location

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/location` | GET | Get saved station latitude/longitude |
| `/api/location` | POST | Set station location (`latitude`, `longitude`) |

### Commands

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/commands/block` | POST | Block inbound TCP commands |
| `/api/commands/unblock` | POST | Unblock inbound TCP commands |

### Profiles

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/profiles/list` | POST | List all saved profiles |
| `/api/profiles/save` | POST | Save current state as profile (`name`, `description`) |
| `/api/profiles/load` | POST | Load a profile (`name`) |
| `/api/profiles/delete` | POST | Delete a profile (`name`) |

## Config file

The config file (`virtual_limits.json` by default) is auto-created and updated on every change. It stores:
- Virtual limit values
- Cable guard settings
- Backend host/port
- Park position
- Station latitude/longitude
- Command block state
- Refresh interval (poll rate)

Profiles are stored as individual `.json` files in a `profiles/` directory next to the config file.

## Running as a service on Raspberry Pi

### 1. Install Python 3.12+

```bash
# Raspberry Pi OS / Debian
sudo apt update
sudo apt install python3 python3-venv
```

### 2. Set up the project

```bash
# Clone the repo
git clone https://github.com/nfacha/RotorVirtualLimit.git /opt/rotor-limit-switch

# Or if you already have the files, make sure the static directory is present
# ls /opt/rotor-limit-switch/static/index.html
```

### 3. Create a systemd service

```bash
sudo nano /etc/systemd/system/rotor-limit-switch.service
```

```
[Unit]
Description=Rotor Virtual Limit Switch Proxy
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/rotor-limit-switch
ExecStart=/usr/bin/python3 /opt/rotor-limit-switch/proxy.py \
    --backend 127.0.0.1:4533 \
    --http-port 8080 \
    --config /opt/rotor-limit-switch/virtual_limits.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 4. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable rotor-limit-switch
sudo systemctl start rotor-limit-switch

# Check status
sudo systemctl status rotor-limit-switch

# View logs
sudo journalctl -u rotor-limit-switch -f
```

### 5. (Optional) Start rotctld as a service too

```
[Unit]
Description=Hamlib rotctld
After=network.target

[Service]
Type=simple
User=pi
ExecStart=/usr/bin/rotctld -m 1 -r /dev/ttyUSB0 -s 9600 -C serial_speed=9600
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable rotctld
sudo systemctl start rotctld
```

### 6. Access the web UI

Open `http://<raspberry-pi-ip>:8080` in a browser.

## Development / Testing

Run the integration tests with a simulated backend (no hardware needed):

```bash
python test_proxy.py --mock
```

To test against real hardware:

```bash
python test_proxy.py
```

## License

MIT
