# Hamlib TCP Proxy Protocol Specification

The Rotor Virtual Limit Switch acts as an inline TCP proxy for the [Hamlib](https://hamlib.github.io/) `rotctld` protocol. It intercepts client connections on TCP port `4534` (default) and forwards validated commands upstream to `rotctld` on TCP port `4533`.

```
┌───────────────────────────┐     Hamlib Protocol     ┌────────────────────────────────┐     Hamlib Protocol     ┌──────────────────────────┐
│ Client (Gpredict/rotctl)  ├────────────────────────►│ Virtual Limit Switch TCP Proxy ├────────────────────────►│  Upstream hamlib rotctld │
│       (Port 4534)         │◄────────────────────────┤          (proxy.py)            │◄────────────────────────┤       (Port 4533)        │
└───────────────────────────┘                         └────────────────────────────────┘                         └──────────────────────────┘
```

---

## Intercepted Commands & Proxy Behavior

The proxy parses incoming Hamlib ASCII protocol lines and applies boundary checks, calibration offsets, and kill-switch state before deciding whether to forward requests upstream.

### 1. Set Position (`P` / `\set_pos`)
* **Syntax:** `P <azimuth> <elevation>` or `\set_pos <azimuth> <elevation>`
* **Validation Pipeline:**
  1. **Command Block (Kill-Switch):** If remote TCP commands are blocked via the Web UI kill-switch, the command is immediately rejected with response `RPRT -15`.
  2. **Software Calibration Offsets:** If offset mode is enabled, software offsets (`az_offset`, `el_offset`) are added to the requested coordinates before checking boundaries.
  3. **Virtual Limit Boundaries:** The target coordinates are checked against `az_min`, `az_max`, `el_min`, and `el_max`. If out of bounds, the proxy rejects the command with response `RPRT -15`.
  4. **Cable Tangling Guard:** Calculates projected net rotation over multi-turn sweeps. If maximum allowed turns would be exceeded, the command is rejected with `RPRT -15`.
  5. **Upstream Forwarding:** If all checks pass, `P <offset_az> <offset_el>` is sent to `rotctld`.
* **Success Response:** `RPRT 0`
* **Rejection Response:** `RPRT -15` (Command Blocked / Limit Exceeded) or `RPRT -11` (Invalid Parameter)

### 2. Get Position (`p` / `\get_pos`)
* **Syntax:** `p` or `\get_pos`
* **Behavior:** Queries the upstream `rotctld` backend for physical hardware coordinates, updates the proxy's internal position state and cable tangling rotation tracker, and returns raw coordinates formatted per Hamlib standard.
* **Response Format:** Two newline-separated floating point numbers:
  ```
  180.000000
  45.000000
  ```

### 3. Move Step (`M` / `\move`)
* **Syntax:** `M <direction> <speed>` or `\move <direction> <speed>`
* **Direction Flags:** Bitwise direction mask (`2` = Up, `4` = Down, `8` = Left, `16` = Right).
* **Behavior:** Passed upstream to `rotctld`. If the backend returns `RPRT -11` (e.g. driver doesn't support smooth stepping), the proxy automatically calculates a target step position and issues a `P` command within safe limit boundaries.

### 4. Emergency Stop (`S` / `\stop`)
* **Syntax:** `S` or `\stop`
* **Behavior:** Immediately forwarded upstream to `rotctld` without boundary checks to halt hardware motion instantly.
* **Response:** `RPRT 0`

### 5. Park Rotator (`K` / `\park`)
* **Syntax:** `K` or `\park`
* **Behavior:** If custom park position coordinates (`park_az`, `park_el`) are saved in virtual limit settings, the proxy translates `K` into a validated `P <park_az> <park_el>` command. Otherwise, `K` is passed through directly to `rotctld`.

### 6. Quit Session (`q` / `\quit`)
* **Syntax:** `q` or `\quit`
* **Behavior:** Closes the active client TCP connection cleanly.

---

## Hamlib Error Codes

The proxy conforms to standard Hamlib return code formats:

| Return Code | Meaning | Cause in Proxy |
|-------------|---------|----------------|
| `RPRT 0` | Success | Command validated and accepted upstream |
| `RPRT -11` | Invalid Parameter | Malformed command syntax or invalid coordinate values |
| `RPRT -15` | Rejected / Denied | Command blocked by virtual limit boundary, cable guard, or Kill-Switch |
| `RPRT -4` | Bus Error / Timeout | Upstream `rotctld` socket closed or unreachable |

---

## Direct CLI Testing & Diagnostics

You can test connection, query status, and verify virtual limit enforcement using standard CLI tools:

### Using `rotctl` (Hamlib Utility)
```bash
# Query position through proxy
rotctl -m 2 -r 127.0.0.1:4534 p

# Move rotator to Azimuth 180°, Elevation 45°
rotctl -m 2 -r 127.0.0.1:4534 P 180.0 45.0

# Attempt move outside virtual limits (e.g. Azimuth 30° when min is 45°)
rotctl -m 2 -r 127.0.0.1:4534 P 30.0 10.0
# Returns: Command line returned error -15
```

### Using `netcat` / `nc`
```bash
# Query position via netcat
echo "p" | nc 127.0.0.1 4534

# Issue position command via netcat
echo "P 180.0 45.0" | nc 127.0.0.1 4534
```
