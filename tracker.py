import json
import logging
import math
import os
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

log = logging.getLogger("tracker")

# ── WGS-84 Constants ──────────────────────────────────
EARTH_R = 6378.137
J2 = 0.00108262998905
J3 = -0.00000253215306
J4 = -0.00000161098761
KE = 0.0743669161
CK2 = 0.5 * J2
CK4 = -0.375 * J4
MIN_PER_DAY = 1440.0

# ── Time helpers ──────────────────────────────────────

def _julian_date(dt):
    y, m = dt.year, dt.month
    if m <= 2:
        y -= 1
        m += 12
    A = math.floor(y / 100)
    B = 2 - A + math.floor(A / 4)
    day = dt.day + (dt.hour + dt.minute / 60 + dt.second / 3600) / 24
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + day + B - 1524.5

def _gstime(jd):
    t = (jd - 2451545.0) / 36525.0
    theta = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t * t * t / 38710000.0
    return math.fmod(theta, 360.0) * math.pi / 180.0

def _days_from_epoch(epoch_yr, epoch_day):
    y = int(epoch_yr)
    if y < 57:
        y += 2000
    else:
        y += 1900
    jan1 = datetime(y, 1, 1, tzinfo=timezone.utc)
    return (datetime(y, 1, 1, tzinfo=timezone.utc) + __import__("datetime").timedelta(days=epoch_day - 1)).replace(tzinfo=timezone.utc)

def _tsince(epoch_jd):
    now = datetime.now(timezone.utc)
    now_jd = _julian_date(now)
    return (now_jd - epoch_jd) * MIN_PER_DAY

# ── TLE Parsing ───────────────────────────────────────

def _tle_checksum(line):
    s = 0
    for c in line[:68]:
        if c.isdigit():
            s += int(c)
        elif c == "-":
            s += 1
    return s % 10

def parse_tle_groups(text):
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    groups = []
    i = 0
    name = None
    while i + 2 <= len(lines):
        if not lines[i].startswith("1 "):
            name = lines[i].strip()
            i += 1
            continue
        l1 = lines[i]
        l2 = lines[i + 1]
        i += 2
        if not l2.startswith("2 "):
            continue
        if len(l1) < 69 or len(l2) < 69:
            continue
        if len(l1) >= 69 and l1[-1].isdigit() and _tle_checksum(l1) != int(l1[-1]):
            log.warning("TLE checksum mismatch for line 1: %s", l1[:68])
        name = (name or f"SAT-{l1[2:7].strip()}")
        groups.append({"name": name, "line1": l1, "line2": l2})
        name = None
    return groups
def parse_tle_line1(line):
    d = {}
    d["norad_id"] = int(line[2:7])
    d["epoch_yr"] = int(line[18:20])
    d["epoch_day"] = float(line[20:32])
    mantissa = line[53:59].strip() or "0"
    exponent = line[59:61].strip() or "0"
    d["bstar"] = float(mantissa) * 10.0 ** (int(exponent) - 5)
    d["ephemeris"] = int(line[62:63])
    d["element_num"] = int(line[64:68])
    return d


def parse_tle_line2(line):
    d = {}
    d["inclination"] = float(line[8:16])
    d["raan"] = float(line[17:25])
    d["eccentricity"] = float("0." + line[26:33])
    d["arg_perigee"] = float(line[34:42])
    d["mean_anomaly"] = float(line[43:51])
    d["mean_motion"] = float(line[52:63])
    d["rev_num"] = int(line[63:68])
    return d

# ── TleSatellite ──────────────────────────────────────

class TleSatellite:
    def __init__(self, raw):
        self.name = raw["name"]
        l1d = parse_tle_line1(raw["line1"])
        l2d = parse_tle_line2(raw["line2"])
        self.norad_id = l1d["norad_id"]
        self.epoch_yr = l1d["epoch_yr"]
        self.epoch_day = l1d["epoch_day"]
        self.bstar = l1d["bstar"]
        self.ephemeris = l1d["ephemeris"]
        self.element_num = l1d["element_num"]
        self.inclination = l2d["inclination"]
        self.raan = l2d["raan"]
        self.eccentricity = l2d["eccentricity"]
        self.arg_perigee = l2d["arg_perigee"]
        self.mean_anomaly = l2d["mean_anomaly"]
        self.mean_motion = l2d["mean_motion"]
        self.rev_num = l2d["rev_num"]
        self.line1 = raw["line1"]
        self.line2 = raw["line2"]
        self._epoch_jd = None
        self._sgp4 = None

    @property
    def epoch_jd(self):
        if self._epoch_jd is None:
            yr = self.epoch_yr
            if yr < 57:
                cy = yr + 2000
            else:
                cy = yr + 1900
            dt = datetime(cy, 1, 1, tzinfo=timezone.utc) + __import__("datetime").timedelta(days=self.epoch_day - 1)
            self._epoch_jd = _julian_date(dt)
        return self._epoch_jd

    def to_dict(self):
        return {
            "name": self.name,
            "norad_id": self.norad_id,
            "inclination": self.inclination,
            "raan": self.raan,
            "eccentricity": self.eccentricity,
            "arg_perigee": self.arg_perigee,
            "mean_anomaly": self.mean_anomaly,
            "mean_motion": self.mean_motion,
            "epoch_yr": self.epoch_yr,
            "epoch_day": self.epoch_day,
        }

# ── SGP4 Propagator ──────────────────────────────────

class SGP4:
    def __init__(self, sat):
        self._sat = sat
        self._init(sat)

    def _init(self, sat):
        deg2rad = math.pi / 180.0
        n0 = sat.mean_motion * 2.0 * math.pi / MIN_PER_DAY
        e0 = sat.eccentricity
        i0 = sat.inclination * deg2rad
        omega0 = sat.arg_perigee * deg2rad
        raan0 = sat.raan * deg2rad
        m0 = sat.mean_anomaly * deg2rad
        bstar = sat.bstar

        a0 = (KE / n0) ** (2.0 / 3.0)
        theta = math.cos(i0)
        sin_i0 = math.sin(i0)
        beta0 = math.sqrt(1.0 - e0 * e0)
        perigee = (a0 * (1.0 - e0) - 1.0) * EARTH_R

        s = 78.0
        if perigee < 98.0:
            s = 20.0
        elif perigee < 156.0:
            s = perigee - 78.0
        s_er = s / EARTH_R
        q0 = 120.0 / EARTH_R

        xi = 1.0 / (a0 - s_er)
        eta = a0 * e0 * xi
        C2 = (q0 - s_er) ** 4 * xi * n0 * (1.0 - eta * eta) ** (-3.5) * (
            a0 * (1.0 + 1.5 * eta * eta + 4.0 * e0 * eta + e0 * eta * eta * eta)
            + 0.75 * CK2 * xi / (1.0 - eta * eta) * (1.5 + 2.0 * e0 * eta + 0.5 * eta * eta * eta + 2.0 * e0 * eta * eta * eta)
        )
        C1 = bstar * C2
        C3 = (q0 - s_er) ** 4 * xi * J3 * n0 * sin_i0 / (CK2 * beta0 * beta0)
        C4 = 2.0 * n0 * (q0 - s_er) ** 4 * xi * a0 * beta0 * beta0 * (1.0 - eta * eta) ** (-3.5) * (
            2.0 * eta * (1.0 + e0 * eta) + 0.5 * e0 + 0.5 * eta * eta * eta
            - 2.0 * CK2 * xi / (a0 * (1.0 - eta * eta))
            * (3.0 * (1.0 - 3.0 * theta * theta) + 3.0 * (3.0 * theta * theta - 1.0) * eta
                - 2.0 * (1.0 - theta * theta) * eta * eta
                - (1.0 - theta * theta) * eta * eta * eta * eta)
        )
        C5 = 2.0 * (q0 - s_er) ** 4 * xi * a0 * beta0 * beta0 * (1.0 - eta * eta) ** (-3.5) * (
            1.0 + 2.75 * (eta * eta + e0 * eta) + e0 * eta * eta * eta + eta * eta * eta * eta
        )
        D2 = 4.0 * a0 * xi * C1 * C1
        D3 = (4.0 / 3.0) * a0 * xi * xi * (17.0 * a0 + s_er) * C1 * C1 * C1
        D4 = (2.0 / 3.0) * a0 * xi * xi * (221.0 * a0 + 31.0 * s_er) * C1 * C1 * C1 * C1

        self._n0 = n0
        self._e0 = e0
        self._i0 = i0
        self._omega0 = omega0
        self._raan0 = raan0
        self._m0 = m0
        self._bstar = bstar
        self._a0 = a0
        self._theta = theta
        self._beta0 = beta0
        self._xi = xi
        self._eta = eta
        self._C1 = C1
        self._C2 = C2
        self._C3 = C3
        self._C4 = C4
        self._C5 = C5
        self._D2 = D2
        self._D3 = D3
        self._D4 = D4
        self._sin_i0 = sin_i0
        self._s_er = s_er
        self._q0_ms4 = (q0 - s_er) ** 4

        n_dot = bstar * self._C2 * 2.0 + bstar * bstar * (self._D2 + self._D3 + self._D4)
        a_dot = -2.0 * a0 * n_dot / (3.0 * n0)

        # Secular rates
        self._omn = n0 + a_dot / 2.0
        self._omg_dot = -0.75 * J2 * n0 * (1.0 - 5.0 * theta * theta) / (beta0 * beta0) + 2.0 * bstar * C1 / (a0 * beta0 * beta0)
        self._raan_dot = 1.5 * J2 * n0 * theta / (beta0 * beta0 * beta0 * beta0)

        self._mean_motion_dot = n_dot
        self._a_dot = a_dot

        self._initialized = True

    def propagate(self, tsince):
        if not self._initialized:
            return (0.0, 0.0, 0.0)

        n0 = self._n0
        e0 = self._e0
        i0 = self._i0
        omega0 = self._omega0
        raan0 = self._raan0
        m0 = self._m0
        theta = self._theta
        beta0 = self._beta0
        xi = self._xi
        eta = self._eta
        C1 = self._C1
        C2 = self._C2
        C3 = self._C3
        C4 = self._C4
        C5 = self._C5
        D2 = self._D2
        D3 = self._D3
        D4 = self._D4
        sin_i0 = self._sin_i0
        s_er = self._s_er
        q0_ms4 = self._q0_ms4

        a_dot = self._a_dot
        n_dot = self._mean_motion_dot
        omn = self._omn
        omg_dot = self._omg_dot
        raan_dot = self._raan_dot

        tsq = tsince * tsince
        tcub = tsq * tsince

        # Secular effects
        m = m0 + omn * tsince + 1.5 * n_dot * tsq + n_dot * n_dot * tsince * tcub / 12.0
        omega = omega0 + omg_dot * tsince + a_dot * C5 * tsince * tsince / 2.0 + (a_dot * C5 * tsince * tsince / 6.0) * tsince
        raan = raan0 + raan_dot * tsince

        a = self._a0 * (1.0 - C1 * tsince - D2 * tsq - D3 * tcub - D4 * tsince * tsq * tsq)

        e = e0 - C1 * tsince - C4 * n_dot * tsq / 2.0

        # Solve Kepler's equation
        m = math.fmod(m, 2.0 * math.pi)
        if m < 0:
            m += 2.0 * math.pi

        e = max(0.0, min(e, 0.999999))
        e2 = e * e
        e3 = e2 * e

        # Newton's method
        E = m
        for _ in range(20):
            dE = (m - (E - e * math.sin(E))) / (1.0 - e * math.cos(E))
            E += dE
            if abs(dE) < 1e-12:
                break

        # True anomaly
        sin_v = math.sqrt(1.0 - e2) * math.sin(E) / (1.0 - e * math.cos(E))
        cos_v = (math.cos(E) - e) / (1.0 - e * math.cos(E))
        v = math.atan2(sin_v, cos_v)

        r = a * (1.0 - e * math.cos(E))
        u = omega + v

        # Short-period perturbations
        u = math.fmod(u, 2.0 * math.pi)
        if u < 0:
            u += 2.0 * math.pi

        cos2u = math.cos(2.0 * u)
        sin2u = math.sin(2.0 * u)

        dr = q0_ms4 * xi * (2.0 / 3.0) / (r * r) * (3.0 * theta * theta - 1.0)
        du = -q0_ms4 * xi * 0.5 * (7.0 * theta * theta - 1.0) / (r * r) * sin2u
        draan = q0_ms4 * xi * 3.0 * theta / (r * r) * cos2u
        di = q0_ms4 * xi * 1.5 * theta * sin_i0 / (r * r) * cos2u

        u += du
        r += dr
        raan += draan
        # Note: di is not applied for positional-only SGP4

        # Position in orbital plane
        x_orb = r * math.cos(u)
        y_orb = r * math.sin(u)

        # Rotate to TEME
        cos_raan = math.cos(raan)
        sin_raan = math.sin(raan)
        cos_i = math.cos(i0)
        sin_i = math.sin(i0)

        x = (cos_raan * math.cos(u) - sin_raan * math.sin(u) * cos_i) * r
        y = (sin_raan * math.cos(u) + cos_raan * math.sin(u) * cos_i) * r
        z = math.sin(u) * sin_i * r

        return (x * EARTH_R, y * EARTH_R, z * EARTH_R)

# ── Coordinate Conversions ────────────────────────────

def _teme_to_geodetic(x, y, z, dt=None):
    a = EARTH_R
    e2 = 0.0066943799901414

    if dt is None:
        dt = datetime.now(timezone.utc)
    jd = _julian_date(dt)
    gst = _gstime(jd)

    xe = x * math.cos(gst) + y * math.sin(gst)
    ye = -x * math.sin(gst) + y * math.cos(gst)
    ze = z

    r = math.sqrt(xe * xe + ye * ye)
    if r < 1e-8:
        lon = 0.0
        lat = math.copysign(math.pi / 2, ze)
    else:
        lon = math.atan2(ye, xe)
        lat = math.atan2(ze, r)
        for _ in range(10):
            sin_lat = math.sin(lat)
            N = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
            new_lat = math.atan2(ze + e2 * N * sin_lat, r)
            if abs(new_lat - lat) < 1e-10:
                lat = new_lat
                break
            lat = new_lat

    sin_lat = math.sin(lat)
    N = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    if abs(lat) < math.pi / 4:
        alt = r / math.cos(lat) - N
    else:
        alt = ze / math.sin(lat) - N * (1.0 - e2)

    return (lat, lon, alt)


def _geodetic_to_az_el(obs_lat, obs_lon, obs_alt, sat_lat, sat_lon, sat_alt):
    a = EARTH_R
    e2 = 0.0066943799901414

    def _llh_to_ecef(lat, lon, alt):
        sin_lat = math.sin(lat)
        cos_lat = math.cos(lat)
        sin_lon = math.sin(lon)
        cos_lon = math.cos(lon)
        N = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
        x = (N + alt) * cos_lat * cos_lon
        y = (N + alt) * cos_lat * sin_lon
        z = (N * (1.0 - e2) + alt) * sin_lat
        return (x, y, z)

    ox, oy, oz = _llh_to_ecef(obs_lat, obs_lon, obs_alt)
    sx, sy, sz = _llh_to_ecef(sat_lat, sat_lon, sat_alt)

    dx = sx - ox
    dy = sy - oy
    dz = sz - oz

    # ECEF to ENU (East-North-Up) at observer
    cos_lat = math.cos(obs_lat)
    sin_lat = math.sin(obs_lat)
    cos_lon = math.cos(obs_lon)
    sin_lon = math.sin(obs_lon)

    east = -dx * sin_lon + dy * cos_lon
    north = -dx * sin_lat * cos_lon - dy * sin_lat * sin_lon + dz * cos_lat
    up = dx * cos_lat * cos_lon + dy * cos_lat * sin_lon + dz * sin_lat

    range_km = math.sqrt(dx * dx + dy * dy + dz * dz)
    if range_km < 1e-8:
        return (0.0, 0.0, 0.0)

    az = math.degrees(math.atan2(east, north))
    if az < 0:
        az += 360.0

    el = math.degrees(math.asin(up / range_km))

    return (az, el, range_km)


def teme_to_az_el(x, y, z, obs_lat_deg, obs_lon_deg, obs_alt_km, dt=None):
    obs_lat = math.radians(obs_lat_deg)
    obs_lon = math.radians(obs_lon_deg)
    sat_lat, sat_lon, sat_alt = _teme_to_geodetic(x, y, z, dt=dt)
    return _geodetic_to_az_el(obs_lat, obs_lon, obs_alt_km, sat_lat, sat_lon, sat_alt)


# ── TLE Source Config ─────────────────────────────────

DEFAULT_TLE_SOURCES = [
    {"url": "http://www.amsat.org/amsat/ftp/keps/current/nasabare.txt", "enabled": True},
]

# ── SatelliteTracker ──────────────────────────────────

CACHE_AGE_MAX = 2 * 3600  # 2 hours — ISS TLE degrades quickly


class SatelliteTracker:
    def __init__(self, limits, config_dir):
        self.limits = limits
        self._cache_path = os.path.join(config_dir, "tle_cache.json")
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        self.sources = list(DEFAULT_TLE_SOURCES)
        self.satellites = []
        self.active = False
        self.target = None
        self.computed_az = None
        self.computed_el = None
        self._sat_lat = None
        self._sat_lng = None
        self._sgp4 = None
        self._epoch_jd = None
        self._last_fetch_time = None
        self._last_fetch_error = None

        self._load_cache()

    def _cache_path_dir(self):
        return os.path.dirname(self._cache_path) or "."

    def _load_cache(self):
        if not os.path.exists(self._cache_path):
            return
        try:
            with open(self._cache_path) as f:
                data = json.load(f)
            self._last_fetch_time = data.get("fetched_at")
            self.sources = data.get("sources", DEFAULT_TLE_SOURCES)
            raw_text = data.get("raw_tle", "")
            if raw_text:
                groups = parse_tle_groups(raw_text)
                self.satellites = [TleSatellite(g) for g in groups]
                log.info("Loaded %d satellites from TLE cache", len(self.satellites))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load TLE cache: %s", e)

    def _save_cache(self, raw_text):
        os.makedirs(self._cache_path_dir(), exist_ok=True)
        data = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sources": self.sources,
            "raw_tle": raw_text,
        }
        try:
            with open(self._cache_path, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            log.warning("Failed to save TLE cache: %s", e)

    def get_fetch_status(self):
        with self._lock:
            age_seconds = None
            age_display = "never"
            stale = True
            downloaded = False
            if self._last_fetch_time:
                try:
                    ft = datetime.fromisoformat(self._last_fetch_time)
                    age_seconds = (datetime.now(timezone.utc) - ft).total_seconds()
                    stale = age_seconds >= CACHE_AGE_MAX
                    age_display = self._format_age(age_seconds)
                    downloaded = True
                except (ValueError, TypeError):
                    pass
            sources = []
            for s in self.sources:
                entry = {
                    "url": s.get("url", ""),
                    "enabled": s.get("enabled", True),
                    "fetched_at": self._last_fetch_time,
                    "age_display": age_display,
                    "age_seconds": age_seconds,
                    "stale": stale,
                    "satellite_count": len(self.satellites),
                    "error": self._last_fetch_error,
                }
                sources.append(entry)
            return {
                "sources": sources,
                "downloaded": downloaded,
                "satellite_count": len(self.satellites),
            }

    def _format_age(self, secs):
        if secs is None:
            return "never"
        if secs < 60:
            return f"{int(secs)}s ago"
        mins = secs / 60
        if mins < 60:
            return f"{int(mins)}m ago"
        hours = mins / 60
        if hours < 48:
            return f"{int(hours)}h ago"
        days = hours / 24
        return f"{int(days)}d ago"

    def fetch(self, force=False):
        with self._lock:
            if not force and self._last_fetch_time:
                try:
                    ft = datetime.fromisoformat(self._last_fetch_time)
                    age = (datetime.now(timezone.utc) - ft).total_seconds()
                    if age < CACHE_AGE_MAX:
                        return len(self.satellites)
                except (ValueError, TypeError):
                    pass

            enabled_sources = [s for s in self.sources if s.get("enabled", True) and s.get("url")]
            if not enabled_sources:
                self._last_fetch_error = "No TLE sources configured"
                return 0

            # Fetch from all enabled sources and merge, newest epoch wins per NORAD ID
            merged: dict[int, TleSatellite] = {}
            combined_raw = ""
            any_success = False
            last_error = None

            for src in enabled_sources:
                url = src["url"]
                try:
                    req = Request(url, headers={"User-Agent": "rotor-vls/1.0"})
                    with urlopen(req, timeout=30) as resp:
                        raw = resp.read().decode()
                    groups = parse_tle_groups(raw)
                    for g in groups:
                        try:
                            sat = TleSatellite(g)
                            existing = merged.get(sat.norad_id)
                            # Keep the one with the newer epoch
                            if existing is None or sat.epoch_jd > existing.epoch_jd:
                                merged[sat.norad_id] = sat
                        except Exception:
                            pass
                    combined_raw += raw + "\n"
                    any_success = True
                    log.info("Fetched %d TLEs from %s", len(groups), url)
                except URLError as e:
                    last_error = str(e)
                    log.warning("TLE fetch failed (%s): %s", url, e)
                except Exception as e:
                    last_error = str(e)
                    log.warning("TLE fetch error (%s): %s", url, e)

            if not any_success:
                self._last_fetch_error = last_error
                return len(self.satellites)

            sats = sorted(merged.values(), key=lambda s: s.name.lower())
            self.satellites = sats
            self._last_fetch_time = datetime.now(timezone.utc).isoformat()
            self._last_fetch_error = None
            self._save_cache(combined_raw)
            log.info("TLE merge complete: %d unique satellites", len(sats))
            return len(sats)

    def find_satellite(self, norad_id):
        for s in self.satellites:
            if s.norad_id == norad_id:
                return s
        return None

    def start(self, satellite):
        with self._lock:
            if self.active:
                self._stop_tracking_locked()
            self.target = satellite
            self.active = True
            self._sgp4 = SGP4(satellite)
            self._epoch_jd = satellite.epoch_jd
            self.limits.set_commands_blocked(True)
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._tracking_loop, daemon=True)
            self._thread.start()
            log.info("Started tracking %s (NORAD %d)", satellite.name, satellite.norad_id)

    def stop(self):
        old_thread = None
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._stop_event.set()
                old_thread = self._thread
                self._thread = None
        if old_thread:
            old_thread.join(timeout=3)
        with self._lock:
            self.active = False
            self.target = None
            self._sgp4 = None
            self.computed_az = None
            self.computed_el = None
            self._sat_lat = None
            self._sat_lng = None
            self.limits.set_commands_blocked(False)
            log.info("Stopped tracking")

    def _stop_tracking_locked(self):
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            thr = self._thread
            self._thread = None
            self._lock.release()
            try:
                thr.join(timeout=3)
            finally:
                self._lock.acquire()
        self.active = False
        self.target = None
        self._sgp4 = None
        self.computed_az = None
        self.computed_el = None
        self._sat_lat = None
        self._sat_lng = None
        self.limits.set_commands_blocked(False)
        log.info("Stopped tracking")

    def _send_goto(self, az, el):
        host = self.limits.backend_host
        port = self.limits.backend_port
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, port))
            s.sendall(f"P {az:.1f} {el:.1f}\n".encode())
            s.close()
        except Exception as e:
            log.debug("tracking send failed: %s", e)

    def _tracking_loop(self):
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    if not self._sgp4 or not self.target:
                        break
                    tsince = (datetime.now(timezone.utc).timestamp() - (self._epoch_jd - 2440587.5) * 86400) / 60.0
                    x, y, z = self._sgp4.propagate(tsince)

                obs_lat = self.limits.latitude
                obs_lon = self.limits.longitude
                if obs_lat is not None and obs_lon is not None:
                    obs_alt = self.limits.altitude if self.limits.altitude is not None else 0.0
                    now = datetime.now(timezone.utc)
                    az, el, _ = teme_to_az_el(x, y, z, obs_lat, obs_lon, obs_alt, dt=now)

                    sat_lat_rad, sat_lon_rad, _ = _teme_to_geodetic(x, y, z, dt=now)
                    with self._lock:
                        self.computed_az = round(az, 1)
                        self.computed_el = round(el, 1)
                        self._sat_lat = round(math.degrees(sat_lat_rad), 4)
                        self._sat_lng = round(math.degrees(sat_lon_rad), 4)

                    if el >= 0:
                        self._send_goto(az, el)
            except Exception as e:
                log.debug("tracking loop error: %s", e)

            self._stop_event.wait(1.0)

    def compute_ground_track(self, sat=None, minutes_ahead=None):
        sat = sat or self.target
        if not sat:
            return []
        sgp4 = SGP4(sat)
        epoch_jd = sat.epoch_jd
        now = datetime.now(timezone.utc)
        now_ts = (now.timestamp() - (epoch_jd - 2440587.5) * 86400) / 60.0
        # Single clean orbit segment (-30 min past to +30 min future)
        points = []
        t = -30.0
        while t <= 30.0:
            tsince = now_ts + t
            x, y, z = sgp4.propagate(tsince)
            dt = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + tsince * 60, tz=timezone.utc)
            try:
                lat, lon, alt = _teme_to_geodetic(x, y, z, dt=dt)
                points.append([round(math.degrees(lat), 4), round(math.degrees(lon), 4)])
            except Exception:
                pass
            t += 1.0
        return points

    def compute_orbit(self, sat=None):
        sat = sat or self.target
        if not sat:
            return []
        sgp4 = SGP4(sat)
        epoch_jd = sat.epoch_jd
        now = datetime.now(timezone.utc)
        now_ts = (now.timestamp() - (epoch_jd - 2440587.5) * 86400) / 60.0
        period_min = (1.0 / sat.mean_motion * MIN_PER_DAY) if sat.mean_motion else 95.0
        half = period_min / 2.0
        points = []
        for i in range(int(-half), int(half)):
            tsince = now_ts + i
            x, y, z = sgp4.propagate(tsince)
            dt = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + tsince * 60, tz=timezone.utc)
            try:
                lat, lon, alt = _teme_to_geodetic(x, y, z, dt=dt)
                points.append([round(math.degrees(lat), 4), round(math.degrees(lon), 4)])
            except Exception:
                continue
        return points

    def compute_upcoming_passes(self, sat=None, obs_lat=None, obs_lon=None, look_ahead_hours=48):
        sat = sat or self.target
        if not sat:
            return []
        if obs_lat is None:
            obs_lat = self.limits.latitude
        if obs_lon is None:
            obs_lon = self.limits.longitude
        if obs_lat is None or obs_lon is None:
            return []

        sgp4 = SGP4(sat)
        epoch_jd = sat.epoch_jd
        now = datetime.now(timezone.utc)
        now_ts = (now.timestamp() - (epoch_jd - 2440587.5) * 86400) / 60.0
        obs_alt = self.limits.altitude if self.limits.altitude is not None else 0.0

        passes = []
        in_pass = False
        aos_time = None
        aos_az = 0.0
        max_el = 0.0

        step = 1
        total = look_ahead_hours * 60
        for i in range(0, total, step):
            tsince = now_ts + i
            x, y, z = sgp4.propagate(tsince)
            dt = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + tsince * 60, tz=timezone.utc)
            try:
                az, el, _ = teme_to_az_el(x, y, z, obs_lat, obs_lon, obs_alt, dt=dt)
            except Exception:
                continue

            if el > 0:
                if not in_pass:
                    aos_time = dt
                    aos_az = az
                    max_el = el
                    in_pass = True
                elif el > max_el:
                    max_el = el
            else:
                if in_pass and aos_time is not None:
                    los_time = dt
                    duration = int((los_time - aos_time).total_seconds() / 60)
                    if duration >= 1:
                        pass_sky = []
                        pass_ground = []
                        t_aos = (aos_time.timestamp() - (epoch_jd - 2440587.5) * 86400) / 60.0
                        t_los = (los_time.timestamp() - (epoch_jd - 2440587.5) * 86400) / 60.0
                        
                        # 1. Sky track for Polar Plot (from AOS to LOS)
                        t_sub = t_aos
                        while t_sub <= t_los:
                            x_s, y_s, z_s = sgp4.propagate(t_sub)
                            dt_sub = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + t_sub * 60, tz=timezone.utc)
                            try:
                                az_s, el_s, _ = teme_to_az_el(x_s, y_s, z_s, obs_lat, obs_lon, obs_alt, dt=dt_sub)
                                if el_s >= 0:
                                    pass_sky.append([round(az_s, 1), round(el_s, 1)])
                            except Exception:
                                pass
                            t_sub += 10.0 / 60.0

                        # 2. Pass Ground Track for Map (single smooth track from AOS to LOS)
                        t_g = t_aos
                        while t_g <= t_los:
                            x_g, y_g, z_g = sgp4.propagate(t_g)
                            dt_g = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + t_g * 60, tz=timezone.utc)
                            try:
                                lat_g, lon_g, _ = _teme_to_geodetic(x_g, y_g, z_g, dt=dt_g)
                                pass_ground.append([round(math.degrees(lat_g), 4), round(math.degrees(lon_g), 4)])
                            except Exception:
                                pass
                            t_g += 10.0 / 60.0

                        passes.append({
                            "id": len(passes),
                            "aos": aos_time.isoformat(),
                            "los": los_time.isoformat(),
                            "max_el": round(max_el, 1),
                            "aos_az": round(aos_az, 1),
                            "los_az": round(az, 1),
                            "duration_min": duration,
                            "sky_track": pass_sky,
                            "ground_track": pass_ground,
                        })
                    in_pass = False
                    aos_time = None

            if len(passes) >= 10:
                break

        return passes

    def compute_sky_track(self, sat=None, obs_lat=None, obs_lon=None):
        sat = sat or self.target
        if not sat:
            return []
        if obs_lat is None:
            obs_lat = self.limits.latitude
        if obs_lon is None:
            obs_lon = self.limits.longitude
        if obs_lat is None or obs_lon is None:
            return []

        sgp4 = SGP4(sat)
        epoch_jd = sat.epoch_jd
        now = datetime.now(timezone.utc)
        now_ts = (now.timestamp() - (epoch_jd - 2440587.5) * 86400) / 60.0
        obs_alt = self.limits.altitude if self.limits.altitude is not None else 0.0

        pass_start_ts = None
        pass_end_ts = None

        # Check current elevation
        x, y, z = sgp4.propagate(now_ts)
        now_dt = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + now_ts * 60, tz=timezone.utc)
        try:
            _, cur_el, _ = teme_to_az_el(x, y, z, obs_lat, obs_lon, obs_alt, dt=now_dt)
        except Exception:
            cur_el = -90.0

        if cur_el >= 0:
            # Active pass! Scan backwards for AOS
            t = now_ts
            while t > now_ts - 35:
                t -= 0.5
                x, y, z = sgp4.propagate(t)
                dt = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + t * 60, tz=timezone.utc)
                try:
                    _, el, _ = teme_to_az_el(x, y, z, obs_lat, obs_lon, obs_alt, dt=dt)
                    if el < 0:
                        pass_start_ts = t
                        break
                except Exception:
                    break
            if pass_start_ts is None:
                pass_start_ts = now_ts - 15

            # Scan forwards for LOS
            t = now_ts
            while t < now_ts + 35:
                t += 0.5
                x, y, z = sgp4.propagate(t)
                dt = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + t * 60, tz=timezone.utc)
                try:
                    _, el, _ = teme_to_az_el(x, y, z, obs_lat, obs_lon, obs_alt, dt=dt)
                    if el < 0:
                        pass_end_ts = t
                        break
                except Exception:
                    break
            if pass_end_ts is None:
                pass_end_ts = now_ts + 15
        else:
            # Next upcoming pass: search ahead up to 24h (1440 min)
            found_aos = False
            for m in range(0, 1440):
                t = now_ts + m
                x, y, z = sgp4.propagate(t)
                dt = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + t * 60, tz=timezone.utc)
                try:
                    _, el, _ = teme_to_az_el(x, y, z, obs_lat, obs_lon, obs_alt, dt=dt)
                except Exception:
                    continue

                if el >= 0 and not found_aos:
                    pass_start_ts = t
                    found_aos = True
                elif el < 0 and found_aos:
                    pass_end_ts = t
                    break

        if pass_start_ts is None or pass_end_ts is None:
            return []

        # High resolution sampling (every 10 seconds = 1/6 minute)
        sky_points = []
        step_min = 10.0 / 60.0
        t = pass_start_ts
        while t <= pass_end_ts:
            x, y, z = sgp4.propagate(t)
            dt = datetime.fromtimestamp((epoch_jd - 2440587.5) * 86400 + t * 60, tz=timezone.utc)
            try:
                az, el, _ = teme_to_az_el(x, y, z, obs_lat, obs_lon, obs_alt, dt=dt)
                if el >= 0:
                    sky_points.append([round(az, 1), round(el, 1)])
            except Exception:
                pass
            t += step_min

        return sky_points

    def get_tracking_status(self, include_extra=False):
        with self._lock:
            result = {
                "active": self.active,
                "satellite": self.target.to_dict() if self.target else None,
                "computed_az": self.computed_az,
                "computed_el": self.computed_el,
                "sat_lat": self._sat_lat,
                "sat_lng": self._sat_lng,
                "below_horizon": self.computed_el is not None and self.computed_el < 0,
            }
            sat = self.target
            obs_lat = self.limits.latitude
            obs_lon = self.limits.longitude
            if include_extra and sat and obs_lat is not None and obs_lon is not None:
                try:
                    ground_track = self.compute_ground_track(sat)
                    result["ground_track"] = ground_track
                except Exception:
                    result["ground_track"] = []
                try:
                    orbit = self.compute_orbit(sat)
                    result["orbit"] = orbit
                except Exception:
                    result["orbit"] = []
                try:
                    passes = self.compute_upcoming_passes(sat, obs_lat, obs_lon)
                    result["passes"] = passes
                except Exception:
                    result["passes"] = []
                try:
                    sky_track = self.compute_sky_track(sat, obs_lat, obs_lon)
                    result["sky_track"] = sky_track
                except Exception:
                    result["sky_track"] = []
            return result

    def get_satellites(self, search=None):
        with self._lock:
            if search:
                q = search.lower().strip()
                return [s.to_dict() for s in self.satellites if q in s.name.lower()]
            return [s.to_dict() for s in self.satellites]
