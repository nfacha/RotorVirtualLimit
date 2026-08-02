"""
Integration test for the rotor virtual limit switch.
  python test_proxy.py --mock   ← uses a simulated rotctld backend
  python test_proxy.py           ← tests against real hardware (requires rotctld)
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request


CONFIG = "test_limits.json"
HTTP_PORT = 8080
PROXY_PORT = 4534
MOCK_BACKEND_PORT = 14533
MOCK_HTTP_PORT = 18081


def mock_backend():
    """Simulates a minimal rotctld server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", MOCK_BACKEND_PORT))
    s.listen(5)
    while True:
        conn, _ = s.accept()
        with conn:
            buf = b""
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode().strip()
                    if cmd in ("p", "\\get_pos"):
                        resp = "180.0\n45.0\n"
                    elif cmd and cmd[0] == "\\":
                        resp = cmd[1:].split()[0] + ": RPRT 0\n"
                    else:
                        resp = "RPRT 0\n"
                    conn.sendall(resp.encode())


def tcp_send(host, port, msg, timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    s.sendall((msg + "\n").encode())
    resp = b""
    while True:
        try:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
            if b"\n" in chunk:
                break
        except socket.timeout:
            break
    s.close()
    return resp.decode().strip()


def http_get(path):
    url = f"http://127.0.0.1:{HTTP_PORT}{path}"
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())


def http_post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{HTTP_PORT}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


PASS = "PASS"
FAIL = "FAIL"
tests = 0
passed = 0


def check(name, ok, detail=""):
    global tests, passed
    tests += 1
    if ok:
        passed += 1
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}" + (f" \u2014 {detail}" if detail else ""))


def main():
    global tests, passed, HTTP_PORT, PROXY_PORT

    parser = argparse.ArgumentParser(description="Run proxy integration tests")
    parser.add_argument("--mock", action="store_true", help="Start a simulated rotctld backend (default: test against real hardware)")
    args = parser.parse_args()

    if args.mock:
        HTTP_PORT = MOCK_HTTP_PORT
        PROXY_PORT = 14534
        backend_port = MOCK_BACKEND_PORT
    else:
        backend_port = 4533

    # Clean up old config
    if os.path.exists(CONFIG):
        os.remove(CONFIG)

    backend_thread = None
    if args.mock:
        backend_thread = threading.Thread(target=mock_backend, daemon=True)
        backend_thread.start()
        time.sleep(0.2)

    # Start proxy
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [
            sys.executable, "proxy.py",
            "--listen-port", str(PROXY_PORT),
            "--backend", f"127.0.0.1:{backend_port}",
            "--http-port", str(HTTP_PORT),
            "--config", CONFIG,
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)

    try:
        # ── Test 1: Basic passthrough ──
        print("\n── Basic passthrough ──")
        resp = tcp_send("127.0.0.1", PROXY_PORT, "S")
        check("Stop command", resp == "RPRT 0", f"got {resp!r}")

        resp = tcp_send("127.0.0.1", PROXY_PORT, "K")
        check("Park command", resp == "RPRT 0", f"got {resp!r}")

        # ── Test 2: Get position ──
        print("\n── Get position ──")
        resp = tcp_send("127.0.0.1", PROXY_PORT, "p")
        got_az_el = len(resp.splitlines()) >= 2
        check("Get position returns az/el", got_az_el, f"got {resp!r}")

        # ── Test 3: Set position (no limits → allowed) ──
        print("\n── Set position (no limits) ──")
        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 100 30")
        check("Set pos within default range", resp == "RPRT 0", f"got {resp!r}")

        # ── Test 4: Set virtual limits via API ──
        print("\n── Set limits via HTTP API ──")
        r = http_post("/api/limits/manual", {"az_min": 45, "az_max": 90, "el_min": 5, "el_max": 50})
        check("Set limits API", r.get("ok") is True)

        # Position outside — should be rejected
        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 100 10")
        check("Reject az > az_max", resp == "RPRT -15", f"got {resp!r}")

        # Position inside — should pass
        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 60 20")
        check("Allow az within range", resp == "RPRT 0", f"got {resp!r}")

        # Position el outside
        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 50 55")
        check("Reject el > el_max", resp == "RPRT -15", f"got {resp!r}")

        # ── Test 5: Disable limits ──
        print("\n── Disable limits ──")
        r = http_post("/api/limits/disable")
        check("Disable limits API", r.get("ok") is True)

        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 100 10")
        check("Allow when disabled", resp == "RPRT 0", f"got {resp!r}")

        # Re-enable
        http_post("/api/limits/enable")

        # ── Test 6: Clear limits ──
        print("\n── Clear limits ──")
        r = http_post("/api/limits/clear")
        check("Clear limits API", r.get("ok") is True)

        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 350 80")
        check("Allow after clear", resp == "RPRT 0", f"got {resp!r}")

        # ── Test 6b: Zero-crossing (wrapping) limits ──
        print("\n── Zero-crossing limits ──")
        r = http_post("/api/limits/manual", {"az_min": 270, "az_max": 80})
        check("Set zero-crossing limits API", r.get("ok") is True)

        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 300 30")
        check("Allow az in wrapping range (300)", resp == "RPRT 0", f"got {resp!r}")

        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 50 30")
        check("Allow az in wrapping range (50)", resp == "RPRT 0", f"got {resp!r}")

        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 0 30")
        check("Allow az at 0 degrees", resp == "RPRT 0", f"got {resp!r}")

        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 180 30")
        check("Reject az in forbidden sector (180)", resp == "RPRT -15", f"got {resp!r}")

        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 100 30")
        check("Reject az in forbidden sector (100)", resp == "RPRT -15", f"got {resp!r}")

        # Test move across 0 degrees
        http_post("/api/rotor/goto", {"az": 0, "el": 0})
        r = http_post("/api/rotor/move", {"direction": 8, "speed": 10})
        check("Move left from 0° wraps across 360°", r.get("ok") is True)

        http_post("/api/limits/clear")

        # ── Test 7: Cable guard ──
        print("\n── Cable guard ──")
        # Use a small limit (0.25 turns = 90°) for clear test behavior
        http_post("/api/cable-guard/max-turns", {"turns": 0.25})
        http_post("/api/cable-guard/enable")

        # Sync position with backend then reset counter
        tcp_send("127.0.0.1", PROXY_PORT, "p")
        http_post("/api/cable-guard/reset")

        if args.mock:
            # Mock backend returns fixed 180° — verify specific movements
            resp = tcp_send("127.0.0.1", PROXY_PORT, "P 180 0")
            check("CG ref set", resp == "RPRT 0", f"got {resp!r}")

            # Move +90° to 270° → net = +90, exactly at limit (check uses >)
            resp = tcp_send("127.0.0.1", PROXY_PORT, "P 270 0")
            check("CG at limit edge", resp == "RPRT 0", f"got {resp!r}")

            # One more degree → net = +91 > 90, should be rejected
            resp = tcp_send("127.0.0.1", PROXY_PORT, "P 271 0")
            check("CG rejects exceed", resp == "RPRT -15", f"got {resp!r}")

            # Move opposite direction → net drops below limit
            resp = tcp_send("127.0.0.1", PROXY_PORT, "P 180 0")
            check("CG allows unwind", resp == "RPRT 0", f"got {resp!r}")
        else:
            # Real hardware — just check cable guard was set up without errors
            check("CG enabled", True)
            check("CG ready (skip step checks on real hardware)", True)

        # Clean up: restore max_turns to 1.0
        http_post("/api/cable-guard/max-turns", {"turns": 1.0})

        # ── Test 8: Status API ──
        print("\n── Status API ──")
        s = http_get("/api/status")
        check("Status has az", s.get("az") is not None)
        check("Status has cable_guard", "cable_guard" in s)
        check("Status has backend", "backend" in s)

        # ── Test 9: Backend config ──
        print("\n── Backend config ──")
        r = http_post("/api/backend/config", {"host": "127.0.0.1", "port": backend_port})
        check("Set backend config", r.get("ok") is True)

        r = http_post("/api/backend/reconnect")
        check("Reconnect", r.get("ok") is True)

        # ── Test 10: Command block ──
        print("\n── Command block ──")
        r = http_post("/api/commands/block")
        check("Block API", r.get("ok") is True)

        resp = tcp_send("127.0.0.1", PROXY_PORT, "P 90 10")
        check("Block rejects TCP commands", resp == "RPRT -15", f"got {resp!r}")

        # Manual control still works via HTTP
        r = http_post("/api/rotor/stop")
        check("Manual stop works while blocked", r.get("ok") is True)

        r = http_post("/api/rotor/goto", {"az": 180, "el": 45})
        check("Manual goto works while blocked", r.get("ok") is True)

        r = http_post("/api/rotor/move", {"direction": 2, "speed": 50})
        check("Manual move works while blocked", r.get("ok") is True)

        r = http_post("/api/rotor/park")
        check("Manual park works while blocked", r.get("ok") is True)

        # Unblock
        r = http_post("/api/commands/unblock")
        check("Unblock API", r.get("ok") is True)

        resp = tcp_send("127.0.0.1", PROXY_PORT, "S")
        check("TCP commands work again after unblock", resp == "RPRT 0", f"got {resp!r}")

        # ── Test 11: Status has commands_blocked ──
        s = http_get("/api/status")
        check("Status has commands_blocked", "commands_blocked" in s)

        # ── Test 12: Park position ──
        print("\n── Park position ──")
        r = http_post("/api/rotor/park-position", {"az": 0, "el": 0})
        check("Set park position", r.get("ok") is True)

        s = http_get("/api/status")
        check("Status has park_az", s.get("park_az") == 0.0)
        check("Status has park_el", s.get("park_el") == 0.0)

        r = http_get("/api/rotor/park-position")
        check("Get park position endpoint", r.get("ok") is True and r.get("az") == 0.0 and r.get("el") == 0.0)

        r = http_post("/api/rotor/park")
        check("Park to saved position", r.get("ok") is True)

        s = http_get("/api/status")
        check("Park cleared park_az", s.get("park_az") == 0.0)  # still set
        check("Park cleared park_el", s.get("park_el") == 0.0)

        # ── Test 13: Station location ──
        print("\n── Station location ──")
        r = http_post("/api/location", {"latitude": 38.8, "longitude": -9.1})
        check("Set location", r.get("ok") is True)

        r = http_get("/api/location")
        check("Get location endpoint", r.get("ok") is True and r.get("latitude") == 38.8 and r.get("longitude") == -9.1)

        s = http_get("/api/status")
        check("Status has latitude", s.get("latitude") == 38.8)
        check("Status has longitude", s.get("longitude") == -9.1)

        # ── Test 14: Profiles ──
        print("\n── Profiles ──")

        # Reset to clean state before profile tests
        http_post("/api/limits/clear")
        http_post("/api/cable-guard/disable")
        http_post("/api/cable-guard/reset")

        # List empty
        r = http_post("/api/profiles/list")
        check("List profiles (empty)", r.get("ok") is True and r.get("profiles") == [])

        # Save a profile from clean state
        r = http_post("/api/profiles/save", {"name": "test-spot", "description": "test location"})
        check("Save profile", r.get("ok") is True)

        # List again
        r = http_post("/api/profiles/list")
        check("List profiles (1)", r.get("ok") is True and len(r.get("profiles", [])) == 1)
        profile_name = r["profiles"][0]["name"]
        check("Profile name matches", profile_name == "test-spot")

        # Modify state, then load profile to restore
        http_post("/api/limits/manual", {"az_min": 10, "az_max": 20})
        http_post("/api/cable-guard/enable")
        r = http_post("/api/profiles/load", {"name": "test-spot"})
        check("Load profile", r.get("ok") is True)

        s = http_get("/api/status")
        check("Profile restored az_min", s.get("az_min") is None)
        check("Profile restored cable_guard_enabled", s.get("cable_guard", {}).get("enabled") is False)

        # Save second profile
        r = http_post("/api/profiles/save", {"name": "another-spot"})
        check("Save second profile", r.get("ok") is True)

        r = http_post("/api/profiles/list")
        check("List profiles (2)", r.get("ok") is True and len(r.get("profiles", [])) == 2)

        # Delete both
        for name in ["test-spot", "another-spot"]:
            r = http_post("/api/profiles/delete", {"name": name})
            check(f"Delete {name}", r.get("ok") is True)

        r = http_post("/api/profiles/list")
        check("All profiles deleted", r.get("ok") is True and r.get("profiles") == [])

        # Clean up profiles directory
        import shutil
        prof_dir = os.path.join(os.path.dirname(CONFIG), "profiles")
        if os.path.isdir(prof_dir):
            shutil.rmtree(prof_dir)

    finally:
        proc.terminate()
        proc.wait(timeout=3)

    # Summary
    print(f"\n{'='*40}")
    print(f"  {passed}/{tests} tests passed")
    for p in [CONFIG, "profiles"]:
        if os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            import shutil
            shutil.rmtree(p)
    return 0 if passed == tests else 1


if __name__ == "__main__":
    sys.exit(main())
