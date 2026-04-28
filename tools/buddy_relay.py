#!/usr/bin/env python3
"""buddy_relay.py — bridge a single permission decision from Claude Code CLI
to a claude-desktop-buddy device, over either BLE or USB serial.

Two transports, picked in this order by default:

1. **BLE** (preferred when no cable). Scans for `Claude-*` advertising
   the Nordic UART Service, connects, writes the prompt, listens for
   the permission response on the TX characteristic. Requires `bleak`.
   Fights for the same single-central slot the Claude Desktop Hardware
   Buddy uses — disconnect that panel before running CLI relay or it
   won't get the slot.

2. **USB serial** (fallback / explicit). Same line-JSON protocol on
   `/dev/cu.wchusbserial*`. Requires `pyserial` and the buddy
   firmware to be in cli mode (settings → input mode → cli).

Override with `--transport ble|usb|auto` or `BUDDY_TRANSPORT=ble|usb|auto`.

Standalone test:

    tools/buddy_relay --tool Bash --hint "ls /tmp"

PreToolUse hook (Claude Code stdin payload, see tools/buddy_relay.md):

    .claude/settings.json wires this script into PreToolUse for the
    tools you want gated through the buddy. Returns
    {"permissionDecision":"allow"} on tap A, "deny" on tap B / timeout.

If neither transport finds a device, exits 0 silently — Claude Code
falls back to its built-in terminal prompt rather than blocking work
when the buddy is unreachable.
"""

import argparse
import asyncio
import glob
import json
import os
import sys
import time
import uuid

DEFAULT_TIMEOUT_S = 60
DEFAULT_BAUD = 115200

NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Sentinel returned by transport functions when they can't reach the
# device (lib missing, no scan match, port absent…). Distinct from
# None which we use elsewhere — caller falls through to next transport.
TRANSPORT_UNAVAILABLE = "transport_unavailable"


# ───────────────────────────── BLE transport ─────────────────────────────

async def _relay_ble(tool: str, hint: str, timeout: float):
    """Send the prompt over BLE via the Nordic UART Service.

    Returns 0 / 2 on a real decision, TRANSPORT_UNAVAILABLE if BLE
    couldn't even be attempted (bleak missing, no advertiser found,
    connection refused).
    """
    try:
        from bleak import BleakScanner, BleakClient
        from bleak.exc import BleakError
    except ImportError:
        print("[buddy] bleak not installed — skipping BLE attempt. "
              "Install with: pipx install bleak", file=sys.stderr)
        return TRANSPORT_UNAVAILABLE

    name_prefix = os.environ.get("BUDDY_NAME_PREFIX", "Claude-")
    pinned_name = os.environ.get("BUDDY_BLE_NAME")
    pinned_addr = os.environ.get("BUDDY_BLE_ADDR")
    scan_s = float(os.environ.get("BUDDY_BLE_SCAN_S", "3"))

    print(f"[buddy] scanning BLE for '{name_prefix}*' ({scan_s:.1f} s)…",
          file=sys.stderr)
    try:
        devices = await BleakScanner.discover(timeout=scan_s)
    except BleakError as e:
        print(f"[buddy] BLE scan failed: {e} — skipping", file=sys.stderr)
        return TRANSPORT_UNAVAILABLE

    target = None
    for d in devices:
        if pinned_addr and d.address.lower() == pinned_addr.lower():
            target = d; break
        if pinned_name and d.name == pinned_name:
            target = d; break
        if not pinned_addr and not pinned_name and d.name and d.name.startswith(name_prefix):
            target = d; break
    if not target:
        print(f"[buddy] no '{name_prefix}*' device advertising — "
              f"is something else (Hardware Buddy?) holding the BLE slot?",
              file=sys.stderr)
        return TRANSPORT_UNAVAILABLE

    print(f"[buddy] connecting to {target.name} ({target.address})…",
          file=sys.stderr)

    pid = uuid.uuid4().hex[:12]
    decision_future: asyncio.Future = asyncio.Future()
    line_buf = bytearray()

    def handle_notify(_handle, data: bytearray):
        line_buf.extend(data)
        while True:
            for sep in (b"\n", b"\r"):
                idx = line_buf.find(sep)
                if idx >= 0:
                    break
            else:
                idx = -1
            if idx < 0:
                return
            line = bytes(line_buf[:idx]).strip()
            del line_buf[:idx + 1]
            if not line.startswith(b"{"):
                continue
            try:
                doc = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if doc.get("cmd") == "permission" and doc.get("id") == pid:
                if not decision_future.done():
                    decision_future.set_result(doc.get("decision", "deny"))

    try:
        async with BleakClient(target, timeout=10.0) as client:
            print("[buddy] GATT connected — discovering services…", file=sys.stderr)

            # Encrypted RX/TX permissions in the firmware (see ble_bridge.cpp:
            # ESP_GATT_PERM_*_ENCRYPTED + auth req SC_MITM_BOND) mean we must
            # bond first. macOS doesn't expose pair() in bleak; the documented
            # workaround is "touch an encrypted attribute and let CoreBluetooth
            # raise the pairing UI". The CCCD on the TX characteristic is
            # encrypted-read, so subscribing to notifications already attempts
            # the encrypted descriptor write — usually enough on its own. We
            # verify by surfacing any GATT error from write_gatt_char below
            # (response=True) instead of letting a write-without-response get
            # silently dropped on the firmware side.
            try:
                await client.start_notify(NUS_TX_UUID, handle_notify)
                print("[buddy] notify subscribed", file=sys.stderr)
            except BleakError as e:
                print(f"[buddy] notify subscribe failed (likely pairing): {e}\n"
                      f"        Watch the Core2 screen for a 6-digit passkey;\n"
                      f"        macOS should prompt you to enter it.", file=sys.stderr)
                return TRANSPORT_UNAVAILABLE

            msg = json.dumps({
                "prompt": {"id": pid, "tool": tool, "hint": hint[:43]},
                "msg": "connected via CLI",
            }) + "\n"
            try:
                await client.write_gatt_char(NUS_RX_UUID, msg.encode(), response=True)
            except BleakError as e:
                print(f"[buddy] write_gatt_char failed: {e}\n"
                      f"        Encrypted-write rejection. If a passkey is on the "
                      f"Core2 screen, enter it in the macOS prompt and rerun.",
                      file=sys.stderr)
                return TRANSPORT_UNAVAILABLE

            print(f"[buddy] prompt sent over BLE: id={pid} tool={tool!r} — "
                  f"waiting up to {timeout:.0f}s for tap on Core2", file=sys.stderr)
            try:
                decision = await asyncio.wait_for(decision_future, timeout=timeout)
                print(f"[buddy] decision: {decision}", file=sys.stderr)
                return 0 if decision in ("once", "always") else 2
            except asyncio.TimeoutError:
                print(f"[buddy] BLE timeout after {timeout:.0f}s — denying.",
                      file=sys.stderr)
                return 2
    except BleakError as e:
        print(f"[buddy] BLE connection failed: {e} — skipping", file=sys.stderr)
        return TRANSPORT_UNAVAILABLE


# ───────────────────────────── USB transport ─────────────────────────────

def _find_usb_port() -> str | None:
    if (env := os.environ.get("BUDDY_PORT")):
        return env
    for pattern in ("/dev/cu.wchusbserial*", "/dev/cu.usbserial*",
                    "/dev/ttyUSB*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def _relay_usb(tool: str, hint: str, timeout: float):
    try:
        import serial
    except ImportError:
        print("[buddy] pyserial not installed — skipping USB attempt. "
              "Install with: pipx install pyserial", file=sys.stderr)
        return TRANSPORT_UNAVAILABLE

    port = _find_usb_port()
    if not port:
        print("[buddy] no USB serial device — skipping USB attempt",
              file=sys.stderr)
        return TRANSPORT_UNAVAILABLE

    pid = uuid.uuid4().hex[:12]
    s = serial.Serial()
    s.port = port
    s.baudrate = DEFAULT_BAUD
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    try:
        s.open()
    except serial.SerialException as e:
        print(f"[buddy] cannot open {port}: {e} — skipping", file=sys.stderr)
        return TRANSPORT_UNAVAILABLE

    try:
        # See module docstring / earlier commits — opening the port
        # toggles DTR briefly, resetting the ESP32. Wait for boot before
        # writing or the prompt lands in a buffer that boot wipes.
        time.sleep(2.5)
        if s.in_waiting:
            s.read(s.in_waiting)

        prompt_msg = json.dumps({
            "prompt": {"id": pid, "tool": tool, "hint": hint[:43]},
            "msg": "connected via CLI",
        })
        s.write((prompt_msg + "\n").encode()); s.flush()
        print(f"[buddy] prompt sent over USB: id={pid} tool={tool!r} — "
              f"waiting up to {timeout:.0f}s for tap on Core2", file=sys.stderr)

        end = time.time() + timeout
        last_resend = time.time()
        buf = b""
        while time.time() < end:
            chunk = s.read(256)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"{"):
                        continue
                    try:
                        doc = json.loads(line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    if doc.get("cmd") == "permission" and doc.get("id") == pid:
                        decision = doc.get("decision", "deny")
                        print(f"[buddy] decision: {decision}", file=sys.stderr)
                        return 0 if decision in ("once", "always") else 2
            if time.time() - last_resend > 1.0:
                s.write((prompt_msg + "\n").encode()); s.flush()
                last_resend = time.time()
        print(f"[buddy] USB timeout after {timeout:.0f}s — denying.",
              file=sys.stderr)
        return 2
    finally:
        try: s.close()
        except Exception: pass


# ───────────────────────────── transport picker ──────────────────────────

def _resolve_transport(arg: str | None) -> str:
    return (arg or os.environ.get("BUDDY_TRANSPORT") or "auto").lower()


def relay(tool: str, hint: str, timeout: float, transport: str) -> int:
    transport = _resolve_transport(transport)
    if transport == "ble":
        rc = asyncio.run(_relay_ble(tool, hint, timeout))
        return 0 if rc is TRANSPORT_UNAVAILABLE else rc
    if transport == "usb":
        rc = _relay_usb(tool, hint, timeout)
        return 0 if rc is TRANSPORT_UNAVAILABLE else rc
    # auto: try BLE first, USB second. Either failing to "find a device"
    # falls through; either reaching the device and getting a real
    # decision (or a real timeout) wins.
    rc = asyncio.run(_relay_ble(tool, hint, timeout))
    if rc is not TRANSPORT_UNAVAILABLE:
        return rc
    rc = _relay_usb(tool, hint, timeout)
    if rc is not TRANSPORT_UNAVAILABLE:
        return rc
    print("[buddy] no transport reached the device — falling back to "
          "Claude Code's default permission flow.", file=sys.stderr)
    return 0


# ───────────────────────────── hook integration ──────────────────────────

def from_hook_stdin() -> tuple[str, str] | None:
    """Read a Claude Code PreToolUse hook payload from stdin (if any)."""
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read().strip()
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    tool = doc.get("tool_name") or doc.get("tool") or "unknown"
    ti = doc.get("tool_input") or {}
    hint = (
        ti.get("command")
        or ti.get("description")
        or ti.get("file_path")
        or ti.get("path")
        or json.dumps(ti)[:200]
    )
    return str(tool), str(hint)


def main() -> int:
    ap = argparse.ArgumentParser(description="Relay a Claude Code permission decision to a claude-desktop-buddy device.")
    ap.add_argument("--tool", help="Tool name shown on the buddy (e.g. Bash, Write).")
    ap.add_argument("--hint", help="Short hint text shown on the buddy.")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"Seconds to wait for a tap before denying (default: {DEFAULT_TIMEOUT_S}).")
    ap.add_argument("--transport", choices=("auto", "ble", "usb"), default=None,
                    help="Override transport. Defaults to auto (BLE first, USB fallback). "
                         "Also reads BUDDY_TRANSPORT env.")
    args = ap.parse_args()

    if args.tool and args.hint:
        return relay(args.tool, args.hint, args.timeout, args.transport)

    hooked = from_hook_stdin()
    if hooked:
        tool, hint = hooked
        rc = relay(tool, hint, args.timeout, args.transport)
        if rc == 0:
            print(json.dumps({"permissionDecision": "allow",
                              "permissionDecisionReason": "buddy approved"}))
        else:
            print(json.dumps({"permissionDecision": "deny",
                              "permissionDecisionReason": "buddy denied or timed out"}),
                  file=sys.stderr)
        return rc

    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
