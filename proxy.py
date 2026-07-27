import argparse
import logging
import os
import signal
import socket
import threading
import time

from limits import RotorLimits
from server import start_http_server

log = logging.getLogger("proxy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)


def parse(line):
    line = line.strip()
    if not line:
        return None
    if line.startswith("\\"):
        parts = line[1:].split(None, 1)
        cmd = parts[0] if parts else ""
        argstr = parts[1] if len(parts) > 1 else ""
    else:
        parts = line.split(None, 1)
        cmd = parts[0] if parts else ""
        argstr = parts[1] if len(parts) > 1 else ""
    args = argstr.split() if argstr else []
    return cmd, args


class Backend:
    def __init__(self, limits):
        self.limits = limits
        self.sock = None

    def _connect(self, host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((host, port))
        s.settimeout(None)
        self.limits.set_backend_reachable(True)
        return s

    @property
    def is_connected(self):
        return self.sock is not None

    def ensure_connected(self):
        """Connect to backend only if not already connected."""
        if self.sock is not None:
            return True
        host = self.limits.backend_host
        port = self.limits.backend_port
        try:
            self.sock = self._connect(host, port)
            log.info("  -> backend %s:%d connected", host, port)
            return True
        except Exception as e:
            log.error("  -> backend %s:%d connect failed: %s", host, port, e)
            self.sock = None
            self.limits.set_backend_reachable(False)
            return False

    def reconnect(self):
        """Force close and reconnect to the backend."""
        self.close()
        return self.ensure_connected()

    def send(self, data):
        if not self.sock:
            return False
        try:
            self.sock.sendall(data.encode())
            return True
        except Exception as e:
            log.error("  -> backend send err: %s", e)
            self.close()
            return False

    def recv(self, timeout=5):
        if not self.sock:
            return None
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(4096)
            if not data:
                self.close()
                return None
            return data.decode()
        except socket.timeout:
            # Timeout is not a fatal error — don't destroy the connection
            return None
        except Exception as e:
            log.error("  -> backend recv err: %s", e)
            self.close()
            return None

    def recv_until(self, min_newlines=1, timeout=2.0):
        """Read until we have min_newlines or timeout/error."""
        if not self.sock:
            return None
        data = b""
        self.sock.settimeout(timeout)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.close()
                    break
                data += chunk
                if data.count(b"\n") >= min_newlines:
                    break
        except socket.timeout:
            # Timeout is not fatal — return whatever we have so far
            pass
        except Exception as e:
            log.error("  -> backend recv_until err: %s", e)
            self.close()
            return data.decode() if data else None
        finally:
            if self.sock:
                self.sock.settimeout(None)
        return data.decode() if data else None

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


class ClientHandler(threading.Thread):
    def __init__(self, client, addr, limits):
        super().__init__(daemon=True)
        self.client = client
        self.addr = addr
        self.limits = limits
        self.backend = Backend(limits)
        self._stop = False

    def run(self):
        log.info("  <- client %s connected", self.addr)
        self.client.settimeout(None)

        buf = ""
        while not self._stop:
            try:
                chunk = self.client.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode()

                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._handle(line)
            except (ConnectionResetError, BrokenPipeError, OSError):
                break

        self._cleanup()

    def _ensure_backend(self):
        """Ensure backend is connected; returns True if ready."""
        if self.backend.ensure_connected():
            return True
        # One retry after a short pause
        time.sleep(0.1)
        return self.backend.reconnect()

    def _backend_exchange(self, data, recv_fn=None):
        """Send data to backend and receive response, with one retry on failure.
        recv_fn: optional custom receive function (e.g. recv_until for get_pos).
        Returns the response string, or None on failure.
        """
        if recv_fn is None:
            recv_fn = self.backend.recv

        if not self._ensure_backend():
            return None

        if not self.backend.send(data):
            # Send failed — connection is broken, try one reconnect
            if not self.backend.reconnect():
                return None
            if not self.backend.send(data):
                return None

        resp = recv_fn()
        if resp is None and not self.backend.is_connected:
            # Connection died during recv — try one reconnect + resend
            if not self.backend.reconnect():
                return None
            if not self.backend.send(data):
                return None
            resp = recv_fn()

        return resp

    def _handle(self, line):
        parsed = parse(line)
        if not parsed:
            return

        if self.limits.commands_blocked:
            log.info("  BLOCKED command from %s (commands blocked)", self.addr)
            self._respond("RPRT -15\n")
            return

        cmd, args = parsed

        if cmd in ("P", "set_pos") and len(args) >= 2:
            self._set_pos(line, args)
        elif cmd in ("M", "move") and len(args) >= 1:
            self._move(line, args)
        elif cmd in ("p", "get_pos"):
            self._get_pos(line)
        else:
            self._passthrough(line)

    def _set_pos(self, line, args):
        try:
            az = float(args[0])
            el = float(args[1])
        except ValueError:
            self._passthrough(line)
            return

        allowed, reason = self.limits.check_position(az, el)
        if not allowed:
            log.info("  REJECT set_pos(%.1f, %.1f) from %s: %s", az, el, self.addr, reason)
            self._respond("RPRT -15\n")
            return

        resp = self._backend_exchange(line + "\n")
        if resp is None:
            self._respond("RPRT -4\n")
            return

        self._respond(resp)
        self.limits.update_position(az, el)

    def _move(self, line, args):
        try:
            direction = int(args[0])
        except ValueError:
            self._passthrough(line)
            return

        allowed, reason = self.limits.check_move_direction(direction)
        if not allowed:
            log.info("  REJECT move(%d) from %s: %s", direction, self.addr, reason)
            self._respond("RPRT -15\n")
            return

        resp = self._backend_exchange(line + "\n")
        self._respond(resp if resp else "RPRT -4\n")

    def _get_pos(self, line):
        resp = self._backend_exchange(
            line + "\n",
            recv_fn=lambda: self.backend.recv_until(3),
        )
        if resp is None:
            self._respond("RPRT -4\n")
            return

        lines = resp.strip().split("\n")
        if len(lines) >= 2 and not (lines[0].startswith("RPRT") if lines else False):
            try:
                az = float(lines[0].strip())
                el = float(lines[1].strip())
                self.limits.update_position(az, el)
            except ValueError:
                pass

        self._respond(resp)

    def _passthrough(self, line):
        resp = self._backend_exchange(line + "\n")
        self._respond(resp if resp else "RPRT -4\n")

    def _respond(self, data):
        try:
            self.client.sendall(data.encode() if isinstance(data, str) else data)
        except Exception:
            self._stop = True

    def _cleanup(self):
        self.backend.close()
        try:
            self.client.close()
        except Exception:
            pass
        log.info("  <- client %s disconnected", self.addr)


_active_handlers = []


def run_proxy(limits):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", limits.proxy_port))
    server.listen(20)
    server.settimeout(1.0)
    log.info("  TCP proxy  -> 0.0.0.0:%d", limits.proxy_port)
    log.info("  Backend    -> %s:%d", limits.backend_host, limits.backend_port)

    running = True

    def shutdown(signum=None, frame=None):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while running:
        try:
            client, addr = server.accept()
            h = ClientHandler(client, addr, limits)
            h.start()
            _active_handlers.append(h)
        except socket.timeout:
            continue
        except OSError:
            break

    log.info("Shutting down...")
    server.close()
    for h in _active_handlers:
        h._stop = True
    for h in _active_handlers:
        h.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description="Rotor Virtual Limit Switch Proxy")
    parser.add_argument("--listen-port", type=int, help="Proxy listen port (default: from config)")
    parser.add_argument("--backend", type=str, help="Backend host:port (default: from config)")
    parser.add_argument("--http-port", type=int, default=8080, help="Web UI port")
    parser.add_argument("--config", default="virtual_limits.json", help="Config file path")
    parser.add_argument("--disabled", action="store_true", help="Start with limits disabled")
    parser.add_argument("--cable-guard", action="store_true", help="Start with cable guard enabled")
    args = parser.parse_args()

    limits = RotorLimits(args.config)

    if args.listen_port:
        limits.proxy_port = args.listen_port
    if args.backend:
        parts = args.backend.split(":")
        limits.set_backend(parts[0], int(parts[1]))
    if args.disabled:
        limits.set_enabled(False)
    if args.cable_guard and not limits.cable_guard_enabled:
        limits.set_cable_guard_enabled(True)

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    start_http_server(limits, static_dir, args.http_port)

    run_proxy(limits)


if __name__ == "__main__":
    main()
