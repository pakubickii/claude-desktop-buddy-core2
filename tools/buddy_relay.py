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
import random
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

DEFAULT_TIMEOUT_S = 60
DEFAULT_BAUD = 115200

# LLM-quote knobs. All optional — feature is OFF unless BUDDY_QUOTE_LLM=1.
DEFAULT_QUOTE_PROB        = 0.05      # ~5% of hook fires generate a quote
DEFAULT_QUOTE_COOLDOWN_S  = 5 * 60    # min seconds between LLM quote sends
DEFAULT_QUOTE_MODEL       = "claude-haiku-4-5-20251001"
QUOTE_MAX_CHARS           = 56        # firmware bubble fits ~14 chars × 4 lines

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


# ───────────────────────────── LLM-quote transport ──────────────────────
#
# Fire-and-forget single-line senders for the speech-bubble quote. Distinct
# from _relay_ble / _relay_usb because the quote send doesn't wait for a
# response — it just writes one JSON frame and disconnects. Used by the
# detached subprocess spawned in `_maybe_spawn_quote`.

async def _send_quote_ble(text: str) -> bool:
    try:
        from bleak import BleakScanner, BleakClient
        from bleak.exc import BleakError
    except ImportError:
        return False
    name_prefix = os.environ.get("BUDDY_NAME_PREFIX", "Claude-")
    pinned_name = os.environ.get("BUDDY_BLE_NAME")
    pinned_addr = os.environ.get("BUDDY_BLE_ADDR")
    scan_s = float(os.environ.get("BUDDY_BLE_SCAN_S", "3"))
    try:
        devices = await BleakScanner.discover(timeout=scan_s)
    except BleakError:
        return False
    target = None
    for d in devices:
        if pinned_addr and d.address.lower() == pinned_addr.lower():
            target = d; break
        if pinned_name and d.name == pinned_name:
            target = d; break
        if not pinned_addr and not pinned_name and d.name and d.name.startswith(name_prefix):
            target = d; break
    if not target:
        return False
    try:
        async with BleakClient(target, timeout=10.0) as client:
            msg = json.dumps({"quote": text[:QUOTE_MAX_CHARS]}) + "\n"
            await client.write_gatt_char(NUS_RX_UUID, msg.encode(), response=True)
            return True
    except BleakError:
        return False


def _send_quote_usb(text: str) -> bool:
    try:
        import serial
    except ImportError:
        return False
    port = _find_usb_port()
    if not port:
        return False
    s = serial.Serial()
    s.port = port
    s.baudrate = DEFAULT_BAUD
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    try:
        s.open()
    except serial.SerialException:
        return False
    try:
        # USB open toggles DTR; the firmware reset wipes any in-flight buffer.
        # Wait for boot before writing — same as the relay path.
        time.sleep(2.5)
        msg = json.dumps({"quote": text[:QUOTE_MAX_CHARS]}) + "\n"
        s.write(msg.encode())
        s.flush()
        # Brief drain so the firmware's line buffer actually sees the write
        # before we close the port (which yanks DTR and resets it again).
        time.sleep(0.4)
        return True
    finally:
        try: s.close()
        except Exception: pass


def _send_quote(text: str) -> bool:
    """Try BLE, fall back to USB. Mirrors the relay's transport order."""
    transport = _resolve_transport(None)
    if transport in ("auto", "ble"):
        if asyncio.run(_send_quote_ble(text)):
            return True
        if transport == "ble":
            return False
    return _send_quote_usb(text)


# ───────────────────────────── LLM quote ─────────────────────────────────

# Few-shot voice anchors. Same vibe as src/quotes.h — keeping them in sync
# manually because pulling from C++ at hook time would be silly. If the
# firmware pool grows substantially, refresh these too.
_QUOTE_VOICE_EXAMPLES = [
    "it works on my machine",
    "regex did nothing wrong",
    "merge conflicts build character",
    "it's not a bug, it's emergent behavior",
    "DNS. it was always DNS.",
    "you can't grep your way out of bad arch",
    "i'm not lazy, i'm async",
    "stack overflow is my therapist",
]


def _quote_cooldown_path() -> Path:
    return Path(tempfile.gettempdir()) / "buddy_quote_lastfire"


def _quote_should_fire() -> bool:
    """Probability + cooldown gate. Fast/synchronous so the relay isn't
    delayed by it — the actual API call happens in a subprocess."""
    if os.environ.get("BUDDY_QUOTE_LLM") != "1":
        return False
    prob = float(os.environ.get("BUDDY_QUOTE_PROB", DEFAULT_QUOTE_PROB))
    if random.random() > prob:
        return False
    cooldown = float(os.environ.get("BUDDY_QUOTE_COOLDOWN_S",
                                    DEFAULT_QUOTE_COOLDOWN_S))
    p = _quote_cooldown_path()
    if p.exists():
        if time.time() - p.stat().st_mtime < cooldown:
            return False
    try:
        p.touch()
    except OSError:
        return False
    return True


def _generate_llm_quote(tool: str, hint: str) -> str | None:
    """Call Anthropic Haiku for a one-line buddy quip about the upcoming
    tool call. Returns None on any failure — caller treats that as
    "no quote this round" and silently skips the send."""
    try:
        import anthropic
    except ImportError:
        print("[buddy] anthropic SDK not installed — pipx install anthropic",
              file=sys.stderr)
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    pet_name = os.environ.get("BUDDY_PET_NAME", "buddy")
    model = os.environ.get("BUDDY_QUOTE_MODEL", DEFAULT_QUOTE_MODEL)

    examples = "\n".join(f"- {q}" for q in _QUOTE_VOICE_EXAMPLES)
    system = (
        f"You are {pet_name}, a desktop hardware buddy that displays a "
        f"single short quip on a tiny screen when a developer runs a "
        f"command. Voice: dry, sassy, geeky, technical, lightly "
        f"self-deprecating. Style anchors:\n{examples}\n\n"
        f"Output ONE line: ≤{QUOTE_MAX_CHARS} ASCII characters, no emojis, "
        f"no surrounding quotes, no trailing punctuation unless it's part "
        f"of the joke. Comment on the developer's action — be specific to "
        f"what they're doing. Output the quip and nothing else."
    )
    user = f"Tool about to run: {tool}\nCommand: {hint[:240]}"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=80,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        print(f"[buddy] LLM call failed: {e}", file=sys.stderr)
        return None

    # Defensive normalisation — strip whitespace, surrounding quotes, and
    # truncate. Models occasionally wrap their output in quotes despite the
    # system prompt, so the .strip("'\"") catches that.
    try:
        text = resp.content[0].text.strip().strip("'\"").strip()
    except (IndexError, AttributeError):
        return None
    if not text:
        return None
    return text[:QUOTE_MAX_CHARS]


def _maybe_spawn_quote(tool: str, hint: str) -> None:
    """Fire-and-forget the LLM-quote pipeline in a detached subprocess.
    Returns immediately so the parent hook can carry on with the
    permission relay. The child handles API call, send, and exit on its
    own — no PID tracked, no waitpid, no shared state past stdin args.

    The probability/cooldown check happens in the parent (cheap, no I/O
    other than a touch on the cooldown file) so we don't burn a process
    fork on the 95% of calls that get filtered out anyway.
    """
    if not _quote_should_fire():
        return
    try:
        subprocess.Popen(
            [sys.executable, __file__, "--llm-quote",
             "--tool", tool, "--hint", hint],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        print(f"[buddy] failed to spawn quote subprocess: {e}", file=sys.stderr)


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

def from_hook_stdin() -> tuple[str, str, str] | None:
    """Read a Claude Code PreToolUse hook payload from stdin (if any).

    Returns (tool, hint, permission_mode). permission_mode is "" if the
    payload predates the field (older Claude Code) — caller treats unknown
    as "fall back to env-var bypass / default to relay".
    """
    if sys.stdin.isatty():
        return None
    raw = sys.stdin.read().strip()
    if not raw:
        return None
    # Diagnostic: dump raw stdin to a file when BUDDY_RELAY_DEBUG_DUMP is set.
    # Used once to discover what fields Claude Code actually passes (e.g.
    # permission_mode). Left in tree as a self-service trace switch.
    dump = os.environ.get("BUDDY_RELAY_DEBUG_DUMP")
    if dump:
        try:
            with open(dump, "a") as fh:
                fh.write(raw + "\n")
        except OSError:
            pass
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
    mode = doc.get("permission_mode") or ""
    return str(tool), str(hint), str(mode)


# Permission modes where Claude Code is *already* running autonomously and
# the user has explicitly opted out of the per-action confirmation loop.
# Relaying these to the buddy would defeat the point of those modes — every
# tool would block on a hardware tap. Instead we silently allow.
_BYPASS_MODES = {"auto", "bypassPermissions", "dontAsk"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Relay a Claude Code permission decision to a claude-desktop-buddy device.")
    ap.add_argument("--tool", help="Tool name shown on the buddy (e.g. Bash, Write).")
    ap.add_argument("--hint", help="Short hint text shown on the buddy.")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"Seconds to wait for a tap before denying (default: {DEFAULT_TIMEOUT_S}).")
    ap.add_argument("--transport", choices=("auto", "ble", "usb"), default=None,
                    help="Override transport. Defaults to auto (BLE first, USB fallback). "
                         "Also reads BUDDY_TRANSPORT env.")
    ap.add_argument("--llm-quote", action="store_true",
                    help="Internal: generate an LLM speech-bubble quote about "
                         "--tool/--hint and send it to the buddy, then exit. "
                         "Used as a detached subprocess by the hook flow; can "
                         "also be invoked manually for testing.")
    args = ap.parse_args()

    # LLM-quote mode bypasses everything else. Spawned by _maybe_spawn_quote
    # with the same --tool/--hint that the relay was invoked with, so the
    # quote can comment on the actual command that's running.
    if args.llm_quote:
        if not args.tool or not args.hint:
            print("[buddy] --llm-quote requires --tool and --hint", file=sys.stderr)
            return 2
        text = _generate_llm_quote(args.tool, args.hint)
        if not text:
            return 0
        ok = _send_quote(text)
        if not ok:
            print(f"[buddy] generated quote {text!r} but no transport reached "
                  f"the device", file=sys.stderr)
        return 0

    if args.tool and args.hint:
        return relay(args.tool, args.hint, args.timeout, args.transport)

    hooked = from_hook_stdin()
    if hooked:
        tool, hint, mode = hooked
        # Mode-aware bypass. Claude Code 1.x sends permission_mode in the
        # PreToolUse payload; we use it to decide whether the user wants
        # per-action approval (relay) or has opted into autonomous flow
        # (bypass). Two env-var overrides on top:
        #
        #   BUDDY_RELAY_DISABLED=1   → always bypass (e.g. CI, scripted runs)
        #   BUDDY_RELAY_FORCE=1      → always relay (override auto mode for
        #                              a buddy-hardware demo session)
        #
        # Older Claude Code that doesn't send permission_mode falls through
        # to the historical default: relay everything. That keeps the
        # hook's behaviour conservative on outdated installs.
        force_off = os.environ.get("BUDDY_RELAY_DISABLED") == "1"
        force_on  = os.environ.get("BUDDY_RELAY_FORCE") == "1"
        bypass = force_off or (mode in _BYPASS_MODES and not force_on)
        # Speech-bubble LLM quote: opportunistic, fires regardless of the
        # bypass/relay path so autonomous-mode users still get the buddy
        # commentary. Spawned detached, doesn't block this hook return.
        _maybe_spawn_quote(tool, hint)
        if bypass:
            why = ("BUDDY_RELAY_DISABLED=1" if force_off
                   else f"permission_mode={mode!r}")
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": f"buddy bypass: {why}",
                }
            }))
            return 0
        rc = relay(tool, hint, args.timeout, args.transport)
        # Claude Code PreToolUse hook contract (current): emit JSON on stdout
        # with hookSpecificOutput wrapping permissionDecision. Flat
        # {"permissionDecision":"allow"} at root is the *deprecated* shape and
        # is silently ignored — Claude Code falls back to its own terminal
        # prompt, which is what the user saw.
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if rc == 0 else "deny",
                "permissionDecisionReason": "buddy approved" if rc == 0
                    else "buddy denied or timed out",
            }
        }
        print(json.dumps(out))
        # Always exit 0 when we have a decision to communicate — non-zero
        # exit codes are reserved for "hook failed", which would surface as
        # an error to the user instead of routing the deny back as a
        # permission decision.
        return 0

    ap.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
