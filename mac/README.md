# ESP32 Agent as an MCP server on the Mac

`esp32_agent_mcp.py` lets Claude (Claude Code / the Claude desktop app on this Mac) see and operate an iPhone
through the ESP32-S3 board, without the iOS app. The board is plugged into the iPhone; the Mac talks to it over
Bluetooth LE using the same protocol as the iOS app (`docs/ble-protocol.md`).

```
iPhone ──Screen Mirroring (board's Wi-Fi hotspot)──▶ ESP32-S3 ──BLE──▶ this Mac: MCP server (launchd, HTTP) ──▶ Claude
   ▲                                                    │
   └──────────── USB HID mouse + keyboard ◀─────────────┘
```

## Why it runs as a launchd agent over HTTP

macOS kills any process that touches CoreBluetooth unless the *responsible* process's app bundle declares
`NSBluetoothAlwaysUsageDescription`. An MCP server spawned by Claude Code over stdio is attributed to the `claude`
CLI, which has no such bundle, so it crashes on the first BLE scan (TCC termination). `install_agent.sh` therefore
builds `ESP32AgentMCP.app` (a copy of Homebrew's Python 3.13 interpreter inside a bundle that declares Bluetooth
use), runs the server from it as a user launchd agent serving MCP over HTTP on `127.0.0.1:8765`, and registers
`http://127.0.0.1:8765/mcp` in `~/.claude.json`.

## One-time setup

1. **Flash the board** (plugged into the Mac): `hardware/flash.sh` (arduino-cli, esp32 core 3.3.12,
   NimBLE-Arduino 2.5.1). The build path must not contain spaces (symlink the repo somewhere space-free if needed).
2. **Install the Mac server**: `brew install python@3.13 uv ffmpeg`, then `mac/install_agent.sh`.
   Re-run it whenever the repo moves (the launchd plist holds absolute paths). `mac/install_agent.sh --uninstall`
   removes everything. Logs: `~/Library/Logs/esp32-agent-mcp.log`. Restart the Claude session once so it
   picks up the new server. Allow the macOS prompt "ESP32 Agent MCP would like to use Bluetooth" if it appears.
3. **On the iPhone** (iOS 18+):
   - Settings → Accessibility → Touch → **AssistiveTouch: on**. The USB mouse then moves an on-screen pointer.
     Also under AssistiveTouch → Pointer Devices, turn **Show Onscreen Keyboard** on so the keyboard still appears
     with the board's USB keyboard attached. Leave tracking speed at the default (calibration assumes it).
   - Settings → Display & Brightness → **Auto-Lock: Never** while using the agent (mirroring stops when the
     phone locks).
4. **Plug the board into the iPhone** (USB-C; Lightning phones need a Lightning-to-USB camera adapter). The phone
   powers the board; it shows up as a USB mouse + keyboard.
5. In Claude, call **`hotspot_info`**: macOS asks to pair with "ESP32 Agent XXXX" (accept). It returns the board's
   Wi-Fi name and password. On the iPhone join that Wi-Fi (Settings → Wi-Fi), then
   **Control Center → Screen Mirroring → ESP32 Agent**. The phone has no internet over that Wi-Fi; it keeps using
   cellular, which is fine because the model runs on the Mac.
6. In Claude, call **`calibrate_start`**, note where the AssistiveTouch pointer (small grey circle) is in the returned
   screenshot, then **`calibrate_move 140 350`** and note it again, then **`calibrate_fit [[60,100,x1,y1],[200,450,x2,y2]]`**.
   Two points are needed: iOS pointer motion has an offset as well as a scale. Saved to `~/.esp32-agent/calibration.json`.

## Tools

| Tool | What it does |
|---|---|
| `status` | board/link/phone health plus hints on what is missing |
| `hotspot_info` | the board's Wi-Fi name and password (pairs the Mac over BLE on first use) |
| `screenshot` | current screen as PNG (default 480×1040; `raw "res W H"` changes it) |
| `calibrate_start` / `calibrate_move` / `calibrate_fit` | two-point pointer calibration (`set_calibration` for scale only) |
| `tap x y`, `long_press x y s`, `swipe x1 y1 x2 y2`, `path [[x,y],…]`, `scroll x y n` | gestures, 0-1000 coords |
| `type_text`, `key` | USB keyboard (ASCII; `enter`, `esc`, `backspace`, `cmd+h`, …) |
| `home`, `back`, `reset_pointer` | Home Screen, back gesture, release + re-home the pointer |
| `raw` | any protocol line from `docs/ble-protocol.md` |

Coordinates are 0-1000 relative to the screenshot, as in `prompts/actor.md`; `prompts/` is good guidance for
how to drive the phone (short swipes, screenshot after every action, type only when a field is focused).

## How the picture stays fresh

The server keeps `stream on` for as long as it is linked and pipes every access unit into one long-lived ffmpeg
(single-threaded, low-delay: anything else parks 14-16 frames in the pipeline), keeping the newest decoded frame.
iOS sends one keyframe per mirroring session and P-frames after it, so after every (re)connect the board first
replays its whole buffer since that keyframe (up to 4 MB, at 100-200 kbps over BLE). `status` → `video.live` says
whether the picture is current; `screenshot` returns a warning with the frame until it is. Restarting Screen
Mirroring on the phone starts a fresh, small buffer and is the fastest way out of a long replay.
The board's buffer is capped at 4 MB. With the firmware in this repo, an overflow while this server is streaming
and caught up drops only the part already received, so the live picture continues indefinitely. `snap`, or a
freshly (re)connected stream, cannot use such a buffer: after a server restart or BLE drop during a long session
the picture stays dark (`status` → `video.waiting_for_keyframe`) until Screen Mirroring is restarted on the phone.

Mirroring stops when the phone locks, and iOS suspends the board's USB link at the same time, so the phone must
stay unlocked. While idle the server nudges the pointer by one unit every 20 s (`ESP32_AGENT_KEEPAWAKE`, 0 = off)
so an unlocked phone never idles to the lock screen; it cannot unlock a locked phone. If mirroring stopped,
`status` says so: choose Control Center → Screen Mirroring → ESP32 Agent again.

## Limits

Same as the iOS app: ~300-500 kbps over BLE, so the mirror is small and ≤5 fps; iOS does not mirror the
passcode screen, Apple Pay or DRM video; mirroring stops when the phone locks; keyboard input is ASCII;
portrait only. The relative pointer drifts, so the server re-homes it every 10 gestures; if taps land off,
re-run the calibration. The board can hold one BLE central at a time per the app's expectations; close the iOS
app's connection if you use both.
