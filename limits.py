import json
import os
import threading


class RotorLimits:
    def __init__(self, config_path="virtual_limits.json"):
        self.config_path = config_path
        self.lock = threading.Lock()

        self.az_min = None
        self.az_max = None
        self.el_min = None
        self.el_max = None
        self.enabled = True

        self.cable_guard_enabled = False
        self.cable_guard_max_turns = 1.0
        self.cable_guard_net_rotation = 0.0
        self.cable_guard_reference_az = None

        self.last_az = None
        self.last_el = None

        self.backend_host = "127.0.0.1"
        self.backend_port = 4533
        self.proxy_port = 4534

        self.park_az = None
        self.park_el = None
        self.latitude = None
        self.longitude = None

        self.commands_blocked = False
        self.refresh_interval_ms = 1000

        self._backend_version = 0
        self._backend_reachable = None  # None=unknown, True, False

        self.load()

    def load(self):
        if not os.path.exists(self.config_path):
            self.save()
            return
        try:
            with open(self.config_path) as f:
                d = json.load(f)
            self.az_min = d.get("az_min")
            self.az_max = d.get("az_max")
            self.el_min = d.get("el_min")
            self.el_max = d.get("el_max")
            self.enabled = d.get("enabled", True)
            self.last_az = d.get("last_az")
            self.last_el = d.get("last_el")
            self.backend_host = d.get("backend_host", "127.0.0.1")
            self.backend_port = d.get("backend_port", 4533)
            self._backend_reachable = None  # not persisted
            self.proxy_port = d.get("proxy_port", 4534)
            self.commands_blocked = d.get("commands_blocked", False)
            self.refresh_interval_ms = d.get("refresh_interval_ms", 1000)
            self.park_az = d.get("park_az")
            self.park_el = d.get("park_el")
            self.latitude = d.get("latitude")
            self.longitude = d.get("longitude")
            cg = d.get("cable_guard", {})
            self.cable_guard_enabled = cg.get("enabled", False)
            self.cable_guard_max_turns = cg.get("max_turns", 1.0)
            self.cable_guard_net_rotation = cg.get("net_rotation", 0.0)
            self.cable_guard_reference_az = cg.get("reference_az")
        except (json.JSONDecodeError, KeyError):
            pass

    def save(self):
        d = {
            "az_min": self.az_min,
            "az_max": self.az_max,
            "el_min": self.el_min,
            "el_max": self.el_max,
            "enabled": self.enabled,
            "last_az": self.last_az,
            "last_el": self.last_el,
            "backend_host": self.backend_host,
            "backend_port": self.backend_port,
            "proxy_port": self.proxy_port,
            "commands_blocked": self.commands_blocked,
            "park_az": self.park_az,
            "park_el": self.park_el,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "cable_guard": {
                "enabled": self.cable_guard_enabled,
                "max_turns": self.cable_guard_max_turns,
                "net_rotation": self.cable_guard_net_rotation,
                "reference_az": self.cable_guard_reference_az,
            },
        }
        with open(self.config_path, "w") as f:
            json.dump(d, f, indent=2)

    def get_status(self):
        with self.lock:
            max_deg = self.cable_guard_max_turns * 360
            net = self.cable_guard_net_rotation
            return {
                "az": self.last_az,
                "el": self.last_el,
                "az_min": self.az_min,
                "az_max": self.az_max,
                "el_min": self.el_min,
                "el_max": self.el_max,
                "enabled": self.enabled,
                "cable_guard": {
                    "enabled": self.cable_guard_enabled,
                    "max_turns": self.cable_guard_max_turns,
                    "net_rotation": round(net, 1),
                    "reference_az": self.cable_guard_reference_az,
                    "tripped": self.cable_guard_enabled
                    and abs(net) >= max_deg,
                    "usage_pct": min(abs(net) / max_deg * 100, 100)
                    if max_deg > 0 and self.cable_guard_enabled
                    else 0,
                },
                "backend": {
                    "host": self.backend_host,
                    "port": self.backend_port,
                    "version": self._backend_version,
                    "reachable": self._backend_reachable,
                },
            "refresh_interval_ms": self.refresh_interval_ms,
            "park_az": self.park_az,
            "park_el": self.park_el,
            "latitude": self.latitude,
            "longitude": self.longitude,
                "proxy_port": self.proxy_port,
                "commands_blocked": self.commands_blocked,
                "refresh_interval_ms": self.refresh_interval_ms,
            }

    def update_position(self, az, el):
        with self.lock:
            if az is not None and self.last_az is not None:
                if self.cable_guard_enabled:
                    delta = _shortest_distance(self.last_az, az)
                    if abs(delta) > 180:
                        self.cable_guard_net_rotation = 0.0
                    else:
                        self.cable_guard_net_rotation += delta
            if az is not None:
                self.last_az = round(az % 360, 1)
            if el is not None:
                self.last_el = round(el % 360, 1)

    def check_position(self, az, el):
        with self.lock:
            if self.enabled:
                if self.az_min is not None and az < self.az_min:
                    return False, "Virtual azimuth left limit reached"
                if self.az_max is not None and az > self.az_max:
                    return False, "Virtual azimuth right limit reached"
                if self.el_min is not None and el < self.el_min:
                    return False, "Virtual elevation down limit reached"
                if self.el_max is not None and el > self.el_max:
                    return False, "Virtual elevation up limit reached"

            if self.cable_guard_enabled and self.last_az is not None:
                delta = _shortest_distance(self.last_az, az)
                new_net = self.cable_guard_net_rotation + delta
                max_deg = self.cable_guard_max_turns * 360
                if abs(new_net) > max_deg:
                    side = "right" if new_net > 0 else "left"
                    return False, f"Cable guard {side} limit exceeded"

            return True, "ok"

    def check_move_direction(self, direction):
        with self.lock:
            if not self.cable_guard_enabled:
                return True, "ok"
            max_deg = self.cable_guard_max_turns * 360
            if direction == 16 and self.cable_guard_net_rotation >= max_deg:
                return False, "Cable guard right limit reached for move"
            if direction == 8 and self.cable_guard_net_rotation <= -max_deg:
                return False, "Cable guard left limit reached for move"
            return True, "ok"

    def capture_limit(self, side):
        with self.lock:
            if side == "left" and self.last_az is not None:
                self.az_min = self.last_az
            elif side == "right" and self.last_az is not None:
                self.az_max = self.last_az
            elif side == "down" and self.last_el is not None:
                self.el_min = self.last_el
            elif side == "up" and self.last_el is not None:
                self.el_max = self.last_el
            else:
                return False
            self.save()
            return True

    def set_limits(self, az_min=None, az_max=None, el_min=None, el_max=None):
        with self.lock:
            if az_min is not None:
                self.az_min = float(az_min)
            if az_max is not None:
                self.az_max = float(az_max)
            if el_min is not None:
                self.el_min = float(el_min)
            if el_max is not None:
                self.el_max = float(el_max)
            self.save()

    def clear_limits(self):
        with self.lock:
            self.az_min = None
            self.az_max = None
            self.el_min = None
            self.el_max = None
            self.save()

    def reset_cable_guard(self):
        with self.lock:
            self.cable_guard_net_rotation = 0.0
            self.cable_guard_reference_az = self.last_az
            self.save()

    def set_cable_guard_enabled(self, enabled):
        with self.lock:
            self.cable_guard_enabled = enabled
            if enabled and self.cable_guard_reference_az is None:
                self.cable_guard_reference_az = self.last_az
            self.save()

    def set_cable_guard_max_turns(self, turns):
        with self.lock:
            self.cable_guard_max_turns = float(turns)
            self.save()

    def set_backend(self, host, port):
        with self.lock:
            self.backend_host = host
            self.backend_port = int(port)
            self._backend_version += 1
            self.save()

    def get_backend_version(self):
        with self.lock:
            return self._backend_version

    def set_backend_reachable(self, ok):
        with self.lock:
            self._backend_reachable = ok

    def set_park_position(self, az, el):
        with self.lock:
            self.park_az = float(az) if az is not None else None
            self.park_el = float(el) if el is not None else None
            self.save()

    def set_location(self, lat, lng):
        with self.lock:
            self.latitude = float(lat) if lat is not None else None
            self.longitude = float(lng) if lng is not None else None
            self.save()

    def set_enabled(self, enabled):
        with self.lock:
            self.enabled = enabled
            self.save()

    def set_commands_blocked(self, blocked):
        with self.lock:
            self.commands_blocked = blocked
            self.save()

    def set_refresh_interval(self, ms):
        with self.lock:
            self.refresh_interval_ms = max(200, int(ms))
            self.save()

    # ── Profile system ─────────────────────────────────

    @property
    def _profiles_dir(self):
        base = os.path.dirname(os.path.abspath(self.config_path))
        return os.path.join(base, "profiles")

    def _profile_path(self, name):
        safe = "".join(c if c.isalnum() or c in " _.-" else "_" for c in name).strip()
        if not safe:
            safe = "unnamed"
        return os.path.join(self._profiles_dir, safe + ".json")

    def _profile_export(self):
        return {
            "limits": {
                "az_min": self.az_min,
                "az_max": self.az_max,
                "el_min": self.el_min,
                "el_max": self.el_max,
                "enabled": self.enabled,
            },
            "cable_guard": {
                "enabled": self.cable_guard_enabled,
                "max_turns": self.cable_guard_max_turns,
                "net_rotation": self.cable_guard_net_rotation,
                "reference_az": self.cable_guard_reference_az,
            },
            "refresh_interval_ms": self.refresh_interval_ms,
            "park_az": self.park_az,
            "park_el": self.park_el,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "backend": {
                "host": self.backend_host,
                "port": self.backend_port,
            },
        }

    def _profile_apply(self, data, bump_backend=True):
        limits = data.get("limits", {})
        self.az_min = limits.get("az_min")
        self.az_max = limits.get("az_max")
        self.el_min = limits.get("el_min")
        self.el_max = limits.get("el_max")
        self.enabled = limits.get("enabled", True)

        cg = data.get("cable_guard", {})
        self.cable_guard_enabled = cg.get("enabled", False)
        self.cable_guard_max_turns = cg.get("max_turns", 1.0)
        self.cable_guard_net_rotation = cg.get("net_rotation", 0.0)
        self.cable_guard_reference_az = cg.get("reference_az")

        self.park_az = data.get("park_az")
        self.park_el = data.get("park_el")
        self.latitude = data.get("latitude")
        self.longitude = data.get("longitude")
        self.refresh_interval_ms = data.get("refresh_interval_ms", 1000)

        be = data.get("backend", {})
        if "host" in be or "port" in be:
            self.backend_host = be.get("host", self.backend_host)
            self.backend_port = int(be.get("port", self.backend_port))
            if bump_backend:
                self._backend_version += 1

    def list_profiles(self):
        import time as _time
        dirp = self._profiles_dir
        if not os.path.isdir(dirp):
            return []
        result = []
        for fn in sorted(os.listdir(dirp)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(dirp, fn)
            try:
                with open(path) as f:
                    d = json.load(f)
                name = d.get("name", fn[:-5])
                desc = d.get("description", "")
                created = d.get("created_at", "")
                updated = d.get("updated_at", "")
                result.append({
                    "name": name,
                    "filename": fn,
                    "description": desc,
                    "created_at": created,
                    "updated_at": updated,
                })
            except (json.JSONDecodeError, OSError):
                result.append({"name": fn[:-5], "filename": fn, "error": True})
        return result

    def save_profile(self, name, description=""):
        import time as _time
        path = self._profile_path(name)
        os.makedirs(self._profiles_dir, exist_ok=True)
        now = _time.strftime("%Y-%m-%dT%H:%M:%S")
        data = self._profile_export()
        data["name"] = name
        data["description"] = description
        data["updated_at"] = now
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
                data["created_at"] = existing.get("created_at", now)
            except (json.JSONDecodeError, OSError):
                data["created_at"] = now
        else:
            data["created_at"] = now
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return True

    def load_profile(self, name):
        path = self._profile_path(name)
        if not os.path.exists(path):
            return False
        with open(path) as f:
            data = json.load(f)
        self._profile_apply(data, bump_backend=True)
        self.save()
        return True

    def delete_profile(self, name):
        path = self._profile_path(name)
        if not os.path.exists(path):
            return False
        os.remove(path)
        return True


def _shortest_distance(a, b):
    diff = (b - a) % 360
    if diff > 180:
        diff -= 360
    return diff
