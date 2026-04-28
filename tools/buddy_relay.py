#!/usr/bin/env python3
"""buddy_relay.py — bridge a single permission decision from Claude Code CLI
to a USB-attached claude-desktop-buddy device.

The standalone `claude` CLI doesn't talk to the Claude Desktop Hardware
Buddy bridge. This script fills the gap by writing the same line-based
JSON protocol the desktop speaks over BLE, but on the buddy's USB serial
port — the firmware accepts both transports interchangeably.

Usage as a standalone tester:

    python3 tools/buddy_relay.py --tool Bash --hint "ls /tmp"

The script blocks until the user taps BtnA (approve) or BtnB (deny) on the
device, then exits 0 / 2 accordingly.

Usage as a Claude Code PreToolUse hook (see tools/buddy_relay.md):

    .claude/settings.json wires this script into PreToolUse for tools you
    want gated through the buddy. The hook contract: exit 0 with a JSON
    permissionDecision on stdout, or exit 2 to block.

Port discovery: BUDDY_PORT env var wins; otherwise the first
/dev/cu.wchusbserial* (or /dev/cu.usbserial*) device is used. If nothing
matches, the script exits 0 silently — i.e. it falls back to Claude
Code's default permission flow rather than blocking work when the buddy
is unplugged.
"""

import argparse
import glob
import json
import os
import sys
import time
import uuid

DEFAULT_TIMEOUT_S = 60
DEFAULT_BAUD = 115200


def find_port() -> str | None:
    """Return the buddy's serial device path, or None if not connected."""
    if (env := os.environ.get("BUDDY_PORT")):
        return env
    for pattern in ("/dev/cu.wchusbserial*", "/dev/cu.usbserial*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def relay(tool: str, hint: str, timeout: float = DEFAULT_TIMEOUT_S) -> int:
    """Send a permission prompt to the buddy and wait for the user's tap.

    Returns:
        0  — user approved (decision == "once" or "always")
        2  — user denied, request timed out, or pyserial / port error
    """
    try:
        import serial  # lazy: only required when a buddy is actually attached
    except ImportError:
        print("[buddy] pyserial not installed; install via 'pip install pyserial'. "
              "Falling back to default permission flow.", file=sys.stderr)
        return 0

    port = find_port()
    if not port:
        print("[buddy] no serial device found — falling back to default "
              "permission flow.", file=sys.stderr)
        return 0

    pid = uuid.uuid4().hex[:12]
    s = serial.Serial()
    s.port = port
    s.baudrate = DEFAULT_BAUD
    s.timeout = 0.2
    s.dtr = False  # never reset the ESP32 on open
    s.rts = False
    try:
        s.open()
    except serial.SerialException as e:
        print(f"[buddy] cannot open {port}: {e} — falling back.", file=sys.stderr)
        return 0

    try:
        # Drop any stale bytes the firmware printed before we attached
        if s.in_waiting:
            s.read(s.in_waiting)

        # Truncate hint to fit the firmware's 43-char buffer (data.h:21)
        prompt_msg = json.dumps({"prompt": {"id": pid, "tool": tool, "hint": hint[:43]}})
        s.write((prompt_msg + "\n").encode())
        s.flush()

        print(f"[buddy] prompt sent: id={pid} tool={tool!r} — waiting up to "
              f"{timeout:.0f}s for tap on Core2 (BtnA=approve, BtnB=deny)",
              file=sys.stderr)

        # The buddy retransmit-loop: every ~1s push the prompt again so a
        # parallel BLE poll from a connected desktop (which clears promptId
        # on every status message that lacks "prompt") can't wipe it.
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
                s.write((prompt_msg + "\n").encode())
                s.flush()
                last_resend = time.time()

        print(f"[buddy] timeout after {timeout:.0f}s — denying.", file=sys.stderr)
        return 2
    finally:
        try:
            s.close()
        except Exception:
            pass


def from_hook_stdin() -> tuple[str, str] | None:
    """Try to read a Claude Code PreToolUse hook payload from stdin.

    Claude Code feeds hook scripts a JSON object on stdin describing the
    tool call. Shape (per current docs):
        {"hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {"command": "...", "description": "..."}}
    Returns (tool, hint) or None if stdin doesn't carry a valid payload.
    """
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
    # Pick the most "showable" field per tool — falls back to a JSON snippet
    hint = (
        ti.get("command")
        or ti.get("description")
        or ti.get("file_path")
        or ti.get("path")
        or json.dumps(ti)[:200]
    )
    return str(tool), str(hint)


def main() -> int:
    ap = argparse.ArgumentParser(description="Relay a Claude Code permission decision to a USB-attached buddy.")
    ap.add_argument("--tool", help="Tool name shown on the buddy (e.g. Bash, Write).")
    ap.add_argument("--hint", help="Short hint text shown on the buddy.")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"Seconds to wait for a tap before denying (default: {DEFAULT_TIMEOUT_S}).")
    args = ap.parse_args()

    if args.tool and args.hint:
        return relay(args.tool, args.hint, args.timeout)

    hooked = from_hook_stdin()
    if hooked:
        tool, hint = hooked
        rc = relay(tool, hint, args.timeout)
        # Claude Code hook contract: emit a JSON permissionDecision so the
        # main loop knows whether to bypass its own permission check.
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
