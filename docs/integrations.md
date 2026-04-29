# Building integrations against the buddy

This is the "how do I plug X into the buddy?" guide. The buddy speaks one
small line-based JSON protocol, and the firmware accepts it on whichever
transport `settings.inputMode` selects:

- `desktop` mode (default) — BLE only. The host that owns the BLE central
  is the only thing the device listens to. Any USB-serial JSON is dropped
  in `dataPoll()` to keep the contract clean.
- `cli` mode — USB serial only. BLE stack isn't even initialised in
  `setup()`, so there's no advertising for a desktop bridge to grab.

Toggle from the device menu (settings → input mode). The setting is
NVS-persistent and the device reboots on toggle so the transport state
applies cleanly.

You don't have to touch firmware to add a new integration. You just need
to deliver the same JSON protocol on whichever transport is active.

## The wire protocol (1-minute version)

Every message is one line of JSON, terminated by `\n`. Lines that don't
start with `{` are ignored. See `REFERENCE.md` for the full schema; the
two messages most integrations care about:

**Set a permission prompt** (host → device):

```json
{"prompt":{"id":"abc123","tool":"Bash","hint":"rm -rf /tmp/cache"}}
```

The device shows the approval screen with that hint until either the
user taps BtnA / BtnB or the host pushes a clearing message:

```json
{"prompt":null}
```

**Permission decision** (device → host):

```json
{"cmd":"permission","id":"abc123","decision":"once"}
```

Decisions are `once`, `always` (currently only `once` is sent — the
firmware doesn't have an "approve and remember" UI yet), or `deny`.

**Status update** (host → device, optional, bidirectional flow):

```json
{"total":5,"running":1,"waiting":1,"tokens":1234,"msg":"editing data.h",
 "entries":["last line A","last line B"]}
```

This populates the `Pet` and `Info` screens. You can send these as
often as you like; absent fields are left untouched. Sending a status
that *omits* `prompt` no longer clears `promptId` (that was the
flickering source for the CLI relay) — explicit `"prompt":null` is the
only way to clear from the host side.

## Pick a transport

### BLE (Nordic UART Service)

Default for desktop integrations. Service UUID
`6e400001-b5a3-f393-e0a9-e50e24dcca9e`, RX char `…0002`, TX char
`…0003`. Single central per peripheral — pick BLE when:

- Your integration runs on the same Mac/Windows box as Claude Desktop
  (use the existing Hardware Buddy bridge, no code).
- Your integration runs anywhere else and can speak BLE GATT
  (Raspberry Pi, phone app, browser via Web Bluetooth).
- You want pairing / encryption / range across the room.

The desktop bridge is one-of-many. Anything that can scan for
`Claude-XXXX` and write to the RX char will work.

### USB serial

Used by `tools/buddy_relay`. Standard CDC-ACM on the CH9102
(`/dev/cu.wchusbserial*` on macOS, `/dev/ttyUSB*` on Linux,
`COMx` on Windows). 115200 baud, no flow control. Pick USB when:

- The integration runs on the same machine as the cable.
- You don't want to fight BLE pairing or the central-slot limit.
- You want determinism — no flaky reconnects, no MTU games.

Caveat: pyserial / cu / picocom asserting DTR on open will reset the
ESP32 (CH9102 ↔ EN line). Either wait ~2.5 s after open before
writing (what `buddy_relay` does), or use a tool that exposes the DTR
flag and pin it low.

## Existing integrations as templates

| Integration | Transport | Direction | Code |
|---|---|---|---|
| Claude Desktop's Hardware Buddy panel | BLE | bidirectional | (closed-source, in the desktop app) |
| `tools/buddy_relay` (Claude Code CLI hook) | USB serial | bidirectional | `tools/buddy_relay.py` |
| `tools/prep_character.py` / `flash_character.py` | USB serial | host → device | repo `tools/` |

The relay is the one to read if you're writing a new host-side
integration: ~150 lines of Python, opens a port, line-buffered JSON,
done. Anything that can write 70 bytes and read a line back can drive
the buddy.

## "Should I write a Claude Code plugin?"

If you want CLI permission prompts on the buddy — no. The relay
already runs as a Claude Code `PreToolUse` hook (see
`.claude/settings.example.json`). Plugins add slash commands /
agents / skills, but for input-gating a hook is the right primitive.

If you want the buddy to react to a non-Claude-Code event source
(IDE, build system, CI bot, smart-home trigger…), you have two
clean paths:

1. **Reuse the wire protocol from your tool's process directly.**
   In cli mode push JSON to `/dev/cu.wchusbserial*`, in desktop mode
   either piggyback on the desktop bridge (no public IPC today, so
   needs reverse engineering) or open your own BLE central.
2. **Write a small daemon.** A long-lived process that owns the
   buddy connection, exposes whatever IPC you like (HTTP, Unix
   socket, named pipe, MQTT), and forwards. That's how you'd
   share the buddy across many short-lived clients without each
   one paying the 2.5 s serial-open cost.

The daemon path is the right move for IDE plugins, web-app
notifications, CI dashboards — anything that wants to talk to the
buddy from many short-lived processes. It's not built yet but the
relay tool is half of it; ~50 more lines of Python turns it into a
local HTTP service.
