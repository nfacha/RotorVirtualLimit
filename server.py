import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse


log = logging.getLogger("server")


_api_limits = None
_static_dir = None
_tracker = None


class APIHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length).decode())
        return {}

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/":
            self._serve_static("index.html")
            return

        if path == "/api/status":
            self._refresh_position()
            status = _api_limits.get_status()
            if _tracker:
                status["tracking"] = _tracker.get_tracking_status(include_extra=False)
                status["tle_sources"] = _tracker.get_fetch_status()
            return self._send_json(status)

        if path == "/api/rotor/park-position":
            self._send_json({
                "ok": True,
                "az": _api_limits.park_az,
                "el": _api_limits.park_el,
            })
            return

        if path == "/api/location":
            self._send_json({
                "ok": True,
                "latitude": _api_limits.latitude,
                "longitude": _api_limits.longitude,
                "altitude": _api_limits.altitude,
            })
            return

        if path == "/api/tracking/status":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            extra = qs.get("extra", [""])[0] in ("1", "true")
            status = _tracker.get_tracking_status(include_extra=extra) if _tracker else {"active": False, "satellite": None}
            self._send_json(status)
            return

        if path == "/api/tracking/sources":
            status = _tracker.get_fetch_status() if _tracker else {"sources": [], "satellite_count": 0}
            self._send_json(status)
            return

        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        try:
            data = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._send_json({"ok": False, "error": "invalid json"}, 400)
            return

        routes = {
            "/api/limits/set-left": lambda: self._ok(_api_limits.capture_limit("left")),
            "/api/limits/set-right": lambda: self._ok(_api_limits.capture_limit("right")),
            "/api/limits/set-up": lambda: self._ok(_api_limits.capture_limit("up")),
            "/api/limits/set-down": lambda: self._ok(_api_limits.capture_limit("down")),
            "/api/limits/enable": lambda: self._ok(_api_limits.set_enabled(True)),
            "/api/limits/disable": lambda: self._ok(_api_limits.set_enabled(False)),
            "/api/limits/clear": lambda: self._ok(_api_limits.clear_limits()),
            "/api/cable-guard/enable": lambda: self._ok(_api_limits.set_cable_guard_enabled(True)),
            "/api/cable-guard/disable": lambda: self._ok(_api_limits.set_cable_guard_enabled(False)),
            "/api/cable-guard/reset": lambda: self._ok(_api_limits.reset_cable_guard()),
            "/api/cable-guard/max-turns": lambda: self._ok(
                _api_limits.set_cable_guard_max_turns(data.get("turns", 1.0))
            ),
            "/api/limits/manual": lambda: self._ok(
                _api_limits.set_limits(
                    az_min=data.get("az_min"),
                    az_max=data.get("az_max"),
                    el_min=data.get("el_min"),
                    el_max=data.get("el_max"),
                )
            ),
            "/api/backend/config": lambda: self._backend_config(data),
            "/api/backend/reconnect": lambda: self._backend_reconnect(),
            "/api/backend/test": lambda: self._backend_test(data),
            "/api/commands/block": lambda: self._ok(_api_limits.set_commands_blocked(True)),
            "/api/commands/unblock": lambda: self._ok(_api_limits.set_commands_blocked(False)),
            "/api/rotor/goto": lambda: self._rotor_goto(data),
            "/api/rotor/move": lambda: self._rotor_move(data),
            "/api/rotor/stop": lambda: self._rotor_cmd("S"),
            "/api/rotor/park": lambda: self._rotor_park(),
            "/api/rotor/park-position": lambda: self._rotor_park_position(data),
            "/api/location": lambda: self._location_set(data),
            "/api/profiles/list": lambda: self._send_json({"ok": True, "profiles": _api_limits.list_profiles()}),
            "/api/profiles/save": lambda: self._profile_save(data),
            "/api/profiles/load": lambda: self._profile_load(data),
            "/api/profiles/delete": lambda: self._profile_delete(data),
            "/api/refresh-interval": lambda: self._ok(
                _api_limits.set_refresh_interval(data.get("ms", 1000))
            ),
            "/api/offset/set": lambda: self._offset_set(data),
            "/api/offset/clear": lambda: self._offset_clear(),
            "/api/offset/enable": lambda: self._offset_enable(),
            "/api/offset/disable": lambda: self._offset_disable(),
            "/api/tracking/fetch": lambda: self._tracking_fetch(data),
            "/api/tracking/start": lambda: self._tracking_start(data),
            "/api/tracking/stop": lambda: self._tracking_stop(),
            "/api/tracking/satellites": lambda: self._tracking_satellites(data),
            "/api/tracking/passes": lambda: self._tracking_preview_passes(data),
        }

        handler = routes.get(path)
        if handler:
            handler()
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def _ok(self, result=True):
        self._send_json({"ok": result if result is not None else True})

    def _backend_config(self, data):
        host = data.get("host", _api_limits.backend_host)
        port = data.get("port", _api_limits.backend_port)
        _api_limits.set_backend(host, port)
        self._send_json({"ok": True})

    def _backend_reconnect(self):
        _api_limits.set_backend(_api_limits.backend_host, _api_limits.backend_port)
        import socket as sck
        try:
            s = sck.socket(sck.AF_INET, sck.SOCK_STREAM)
            s.settimeout(5)
            s.connect((_api_limits.backend_host, _api_limits.backend_port))
            s.close()
            _api_limits.set_backend_reachable(True)
            self._send_json({"ok": True})
        except Exception:
            _api_limits.set_backend_reachable(False)
            self._send_json({"ok": False, "error": "connection failed"})

    def _backend_test(self, data):
        host = data.get("host", _api_limits.backend_host)
        port = data.get("port", _api_limits.backend_port)
        import socket as sck
        try:
            s = sck.socket(sck.AF_INET, sck.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, port))
            s.sendall(b"p\n")
            r = s.recv(4096).decode()
            s.close()
            ok = bool(r.strip())
            _api_limits.set_backend_reachable(True)
            self._send_json({"ok": ok, "reachable": True, "response": r.strip()[:80]})
        except Exception as e:
            _api_limits.set_backend_reachable(False)
            self._send_json({"ok": False, "reachable": False, "error": str(e)})

    def _backend_send(self, cmd):
        import socket as sck
        host = _api_limits.backend_host
        port = _api_limits.backend_port
        try:
            s = sck.socket(sck.AF_INET, sck.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, port))
            _api_limits.set_backend_reachable(True)
            s.sendall((cmd + "\n").encode())
            resp = b""
            s.settimeout(0.5)
            while True:
                try:
                    c = s.recv(4096)
                    if not c:
                        break
                    resp += c
                except sck.timeout:
                    break
            s.close()
            return resp.decode().strip()
        except Exception as e:
            _api_limits.set_backend_reachable(False)
            return f"RPRT -4"

    def _refresh_position(self):
        import socket as sck
        host = _api_limits.backend_host
        port = _api_limits.backend_port
        try:
            s = sck.socket(sck.AF_INET, sck.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, port))
            s.sendall(b"p\n")
            resp = b""
            while True:
                c = s.recv(4096)
                if not c:
                    break
                resp += c
                if resp.count(b"\n") >= 2:
                    break
            s.close()
            lines = resp.decode().strip().split("\n")
            if len(lines) >= 2:
                az = float(lines[0].strip())
                el = float(lines[1].strip())
                _api_limits.update_position(az, el)
        except Exception:
            pass

    def _rotor_goto(self, data):
        az = data.get("az")
        el = data.get("el")
        if az is None and el is None:
            self._send_json({"ok": False, "error": "az or el required"}, 400)
            return
        if az is None:
            az = _api_limits.last_az or 0
        if el is None:
            el = _api_limits.last_el or 0
        az = float(az) % 360
        el = max(0, min(90, float(el)))
        # Apply offset before limits check
        if _api_limits.offset_enabled:
            offset_az = az + _api_limits.az_offset
            offset_el = el + _api_limits.el_offset
        else:
            offset_az = az
            offset_el = el
        allowed, reason = _api_limits.check_position(offset_az, offset_el)
        if not allowed:
            self._send_json({"ok": False, "error": reason, "response": "RPRT -15"})
            return
        resp = self._backend_send(f"P {offset_az} {offset_el}")
        _api_limits.update_position(offset_az, offset_el)
        ok = "RPRT 0" in resp
        self._refresh_position()
        self._send_json({"ok": ok, "response": resp})

    def _rotor_move(self, data):
        direction = data.get("direction")
        speed = data.get("speed")
        if direction is None:
            self._send_json({"ok": False, "error": "direction required"}, 400)
            return
        cmd = f"M {int(direction)}"
        if speed is not None:
            cmd += f" {int(speed)}"
        resp = self._backend_send(cmd)
        ok = "RPRT 0" in resp
        if not ok and "RPRT -11" in resp:
            self._refresh_position()
            step = max(0.5, float(speed or 5))
            az_off = el_off = 0.0
            if direction & 8:
                az_off = -step
            if direction & 16:
                az_off = step
            if direction & 2:
                el_off = step
            if direction & 4:
                el_off = -step
            az = (_api_limits.last_az or 0) + az_off
            el = (_api_limits.last_el or 0) + el_off
            az = max(0, min(360, az))
            el = max(0, min(90, el))
            # Apply offset before limits check
            if _api_limits.offset_enabled:
                offset_az = az + _api_limits.az_offset
                offset_el = el + _api_limits.el_offset
            else:
                offset_az = az
                offset_el = el
            allowed, reason = _api_limits.check_position(offset_az, offset_el)
            if not allowed:
                self._send_json({"ok": False, "error": reason, "response": "RPRT -15"})
                return
            resp = self._backend_send(f"P {offset_az} {offset_el}")
            ok = "RPRT 0" in resp
        self._refresh_position()
        self._send_json({"ok": ok, "response": resp})

    def _rotor_cmd(self, cmd):
        resp = self._backend_send(cmd)
        ok = "RPRT 0" in resp or resp.startswith("RPRT 0")
        if not ok and "RPRT -11" in resp:
            ok = True
        self._refresh_position()
        self._send_json({"ok": ok, "response": resp})

    def _rotor_park(self):
        az = _api_limits.park_az
        el = _api_limits.park_el
        if az is not None or el is not None:
            self._rotor_goto({"az": az, "el": el})
        else:
            self._rotor_cmd("K")

    def _rotor_park_position(self, data):
        az = data.get("az")
        el = data.get("el")
        if az is None and el is None:
            self._send_json({"ok": False, "error": "az or el required"}, 400)
            return
        _api_limits.set_park_position(az, el)
        self._send_json({"ok": True})

    def _location_set(self, data):
        lat = data.get("latitude")
        lng = data.get("longitude")
        alt = data.get("altitude")
        if lat is None and lng is None:
            self._send_json({"ok": False, "error": "latitude or longitude required"}, 400)
            return
        _api_limits.set_location(lat, lng, alt)
        self._send_json({"ok": True})

    def _offset_set(self, data):
        az = data.get("az_offset")
        el = data.get("el_offset")
        old_az = _api_limits.az_offset
        old_el = _api_limits.el_offset
        _api_limits.set_offset(az=az, el=el)
        new_az = _api_limits.az_offset
        new_el = _api_limits.el_offset
        if _api_limits.offset_enabled:
            self._offset_move_rotor(new_az - old_az, new_el - old_el)
        self._send_json({"ok": True})

    def _offset_clear(self):
        old_az = _api_limits.az_offset
        old_el = _api_limits.el_offset
        _api_limits.clear_offset()
        if _api_limits.offset_enabled:
            self._offset_move_rotor(-old_az, -old_el)
        self._send_json({"ok": True})

    def _offset_enable(self):
        _api_limits.set_offset_enabled(True)
        self._offset_move_rotor(_api_limits.az_offset, _api_limits.el_offset)
        self._send_json({"ok": True})

    def _offset_disable(self):
        old_az = _api_limits.az_offset
        old_el = _api_limits.el_offset
        _api_limits.set_offset_enabled(False)
        self._offset_move_rotor(-old_az, -old_el)
        self._send_json({"ok": True})

    def _offset_move_rotor(self, az_delta, el_delta):
        """Move the rotor by the given deltas to reflect an offset change."""
        if az_delta == 0 and el_delta == 0:
            return
        az = _api_limits.last_az
        el = _api_limits.last_el
        if az is None:
            if el is None:
                log.info("  offset move skipped — no position known")
                return
            az = el
        if el is None:
            el = az
        target_az = round(az + az_delta, 1)
        target_el = round(el + el_delta, 1)
        target_az = max(0, min(360, target_az))
        target_el = max(0, min(90, target_el))
        allowed, _ = _api_limits.check_position(target_az, target_el)
        if not allowed:
            log.info("  offset move skipped — limits reject %.1f %.1f", target_az, target_el)
            return
        resp = self._backend_send(f"P {target_az} {target_el}")
        log.info("  offset move P %.1f %.1f -> %s", target_az, target_el, resp)
        if "RPRT 0" in resp or not resp.startswith("RPRT -"):
            _api_limits.update_position(target_az, target_el)

    # ── Profiles ────────────────────────────────────────

    def _profile_save(self, data):
        name = data.get("name", "").strip()
        if not name:
            self._send_json({"ok": False, "error": "name required"}, 400)
            return
        desc = data.get("description", "")
        ok = _api_limits.save_profile(name, desc)
        self._send_json({"ok": ok})

    def _profile_load(self, data):
        name = data.get("name", "").strip()
        if not name:
            self._send_json({"ok": False, "error": "name required"}, 400)
            return
        ok = _api_limits.load_profile(name)
        self._send_json({"ok": ok})

    def _profile_delete(self, data):
        name = data.get("name", "").strip()
        if not name:
            self._send_json({"ok": False, "error": "name required"}, 400)
            return
        ok = _api_limits.delete_profile(name)
        self._send_json({"ok": ok})

    def _tracking_fetch(self, data):
        if not _tracker:
            self._send_json({"ok": False, "error": "tracker not available"})
            return
        force = data.get("force", True)
        count = _tracker.fetch(force=force)
        self._send_json({"ok": True, "count": count})

    def _tracking_start(self, data):
        if not _tracker:
            self._send_json({"ok": False, "error": "tracker not available"})
            return
        norad_id = data.get("norad_id")
        if norad_id is None:
            self._send_json({"ok": False, "error": "norad_id required"})
            return
        sat = _tracker.find_satellite(norad_id)
        if not sat:
            self._send_json({"ok": False, "error": "satellite not found"})
            return
        if _api_limits.latitude is None or _api_limits.longitude is None:
            self._send_json({"ok": False, "error": "set station location first"})
            return
        _tracker.start(sat)
        _api_limits.set_tracking_target(sat.to_dict())
        _api_limits.set_tracking_active(True)
        self._send_json({"ok": True, "satellite": sat.name})

    def _tracking_stop(self):
        if _tracker:
            _tracker.stop()
            _api_limits.set_tracking_target(None)
            _api_limits.set_tracking_active(False)
        self._send_json({"ok": True})

    def _tracking_satellites(self, data):
        if not _tracker:
            self._send_json({"ok": False, "error": "tracker not available"})
            return
        search = data.get("search", "")
        sats = _tracker.get_satellites(search=search)
        self._send_json({"ok": True, "satellites": sats})

    def _tracking_preview_passes(self, data):
        if not _tracker:
            self._send_json({"ok": False, "error": "tracker not available"})
            return
        norad_id = data.get("norad_id")
        if norad_id is None:
            self._send_json({"ok": False, "error": "norad_id required"})
            return
        sat = _tracker.find_satellite(norad_id)
        if not sat:
            self._send_json({"ok": False, "error": "satellite not found"})
            return
        obs_lat = _api_limits.latitude
        obs_lon = _api_limits.longitude
        if obs_lat is None or obs_lon is None:
            self._send_json({"ok": False, "error": "set station location first"})
            return
        passes = _tracker.compute_upcoming_passes(sat, obs_lat, obs_lon)
        sky_track = _tracker.compute_sky_track(sat, obs_lat, obs_lon)
        ground_track = _tracker.compute_ground_track(sat)
        self._send_json({
            "ok": True,
            "satellite": sat.name,
            "passes": passes,
            "sky_track": sky_track,
            "ground_track": ground_track
        })

    def _serve_static(self, name):
        for base in (_static_dir, os.path.join(os.getcwd(), "static")):
            path = os.path.join(base, name)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content)
                return
        self.send_error(404, f"static/{name} not found")


def start_http_server(limits, static_dir: str, port: int = 8080, tracker=None):
    global _api_limits, _static_dir, _tracker
    _api_limits = limits
    _static_dir = static_dir
    _tracker = tracker

    server = HTTPServer(("0.0.0.0", port), APIHandler)
    print(f"  Web UI    -> http://localhost:{port}")

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
