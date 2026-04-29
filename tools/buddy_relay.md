# `buddy_relay.py` — relay Claude Code CLI permission prompts to the buddy

The standalone `claude` CLI shows permission prompts in the terminal and
doesn't talk to the Hardware Buddy bridge in Claude Desktop. This script
bridges the gap by speaking the same line-based JSON protocol to the
buddy over USB serial — the firmware accepts the same wire format on
both transports, so a CLI session can route taps through the device
just like the desktop does over BLE.

## Requirements

- Buddy plugged into the host via USB-C (CH9102 driver loaded — you
  should see `/dev/cu.wchusbserial*`).
- A Python 3.10+ interpreter with `pyserial` available somewhere on the
  machine. The launcher (`tools/buddy_relay`) auto-discovers a usable
  one — PlatformIO's bundled Python ships with `pyserial` so just
  having PIO installed is enough.
- If neither PlatformIO nor any system Python has `pyserial`:
  `pipx install pyserial && pipx ensurepath`, or
  `pip3 install --user --break-system-packages pyserial`, or set
  `BUDDY_PYTHON=/path/to/python` to override discovery.

## Standalone test (do this first)

```bash
tools/buddy_relay --tool Bash --hint "ls /tmp"
```

Watch the Core2: an APPROVAL screen with id, tool name, and hint should
appear. **BtnA** = approve → script exits 0. **BtnB** = deny → exits 2.
After ~60 s with no tap the script gives up and exits 2.

If `/dev/cu.wchusbserial*` is not present (buddy unplugged, no driver),
the script exits 0 silently and lets Claude Code's default permission
flow take over — never blocks work because of an absent device.

Override the auto-detected port with `BUDDY_PORT=/dev/cu.wchusbserialXYZ`.

## Wire it into Claude Code as a `PreToolUse` hook

Once the standalone test works, copy the block from
[`.claude/settings.example.json`](../.claude/settings.example.json)
into either the project's `.claude/settings.json` or your user-global
`~/.claude/settings.json` and restart Claude Code.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/tools/buddy_relay",
            "timeout": 90
          }
        ]
      }
    ]
  }
}
```

`$CLAUDE_PROJECT_DIR` is set by Claude Code to the repo root; for a
user-global config, replace with the absolute path. The 90 s `timeout`
matches `buddy_relay`'s default 60 s decision window with headroom for
the boot-settle delay.

When Claude Code is about to run a matching tool it pipes the tool-call
metadata to the script on stdin; the script forwards it to the buddy,
waits for a tap, and emits one of:

- `{"permissionDecision":"allow","permissionDecisionReason":"buddy approved"}`
  on stdout + exit 0 — Claude Code skips its terminal prompt and runs
  the tool.
- `{"permissionDecision":"deny","permissionDecisionReason":"buddy denied or timed out"}`
  on stderr + exit 2 — Claude Code blocks the tool and shows the reason.

## Concurrency with Claude Desktop

The buddy firmware processes USB-serial JSON and BLE JSON in the same
loop. If Claude Desktop is paired and polling status over BLE, those
status updates clear `promptId` (the firmware treats every JSON line
without a `prompt` field as a "no pending prompt" signal).

`buddy_relay.py` works around this by **retransmitting the prompt every
~1 s** until the user taps or the timeout fires. In practice this means
the prompt screen survives concurrent desktop polling — there might be
a brief flicker but it stays usable.

If you'd rather have a clean separation: in Hardware Buddy in Desktop
click **Disconnect** before using the CLI relay.

## Why USB serial, not BLE?

BLE GATT only allows one Central per Peripheral at a time. Desktop
holds that slot when paired. USB serial is a separate transport the
firmware already accepts (`_usbLine.feed(Serial, out)` in `data.h`), so
we can run the CLI relay alongside an active desktop connection without
unpairing.

## Limitations / next steps

- **One-shot per call**: if Claude Code triggers many tool calls in
  rapid succession, each opens its own serial port. Should be fine for
  typical interactive use; could collide if calls overlap. A long-lived
  daemon that owns the port is a future improvement.
- **No "always allow" memory**: every tool call asks again. Native
  Claude Code permission caching ("yes, and don't ask again") is
  bypassed because the hook returns a per-call decision. If you want
  per-tool whitelisting, add that logic before invoking `relay()`.
- **Hint truncation**: the firmware's `promptHint` buffer is 43 chars
  (`data.h:21`); the script trims to fit. If you want richer prompts,
  bump that buffer in the firmware and rebuild.
