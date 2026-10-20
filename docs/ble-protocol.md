# BLE protocol (ESP32 ⇄ iOS app)

The board is a BLE peripheral named `ESP32 Agent XXXX` (XXXX = last 2 MAC bytes, hex).
Security: LE Secure Connections, bonding, Just Works. `net` requires an encrypted link,
so reading it triggers the iOS pairing prompt.

Service `6e400001-a9e8-4b1b-9c39-6d6f8a3c0000`

| Characteristic | UUID suffix | Props | Purpose |
|---|---|---|---|
| `cmd`    | `...0001` | write, write-no-rsp | one command line, UTF-8, `<id> <command>` |
| `reply`  | `...0002` | notify | `<id> ok` or `<id> err <reason>` |
| `status` | `...0003` | notify, read | JSON heartbeat, every 1 s (also keeps the app alive in background) |
| `video`  | `...0004` | notify | H.264 chunks, see below |
| `net`    | `...0005` | read, write (encrypted) | network config JSON |

Full UUID = `6e400001-a9e8-4b1b-9c39-6d6f8a3c` + 4-hex suffix, e.g. cmd = `6e400001-a9e8-4b1b-9c39-6d6f8a3c0001`.

## cmd
`<id>` is a decimal request id chosen by the app; the reply echoes it.
Existing HID commands, unchanged semantics from the old TCP protocol:
`ping`, `home`, `move DX DY`, `press`, `release`, `click`, `scroll A B`, `type TEXT`, `key COMBO`.
New:
- `stream on [TS]|off` — continuously forward every access unit over `video` (default off). `stream on`
  starts with CONFIG and the whole GOP since the IDR. `stream on TS`, from a client that already holds every
  AU up to board time TS (the Mac keeps its copy on disk), continues with the first AU newer than TS and
  sends no CONFIG and no replay. The board falls back to a full replay when its buffer restarted after TS
  (a new IDR), or when TS is in the future (another boot); into a continuation whose start the client never
  received it sends nothing until the next IDR (logged).
- `snap` — send SPS/PPS + every AU since the last IDR (the current GOP), then reply `ok`.
  Replies `err no video (buffer overflowed, waiting for a keyframe: ...)` while the board's buffer is a
  continuation (see below): it cannot be decoded from its first AU.
- `res W H` — AirPlay resolution advertised in `/info` (default 480 1040), stored in NVS; takes effect on the next mirroring session.
- `fps N` — max fps advertised (default 5), stored in NVS.
- `bench KB` — send KB kilobytes of filler on `video` with the `bench` flag, then reply `ok <ms>`.
- `unpair` — erase all bonds and restart advertising.
- `coex [bt|balance|wifi]` — Wi-Fi/BLE radio-share preference (A/B knob, not stored; every boot starts at
  `balance`). Replies `ok <current>`; bare `coex` just reports it.
- `pace STEP STEP_MS` — pointer move pacing: 1-127 units per report, 1-50 ms between reports (not stored).
- `usbreset` — replies `ok`, then disconnects and reconnects the USB device so the phone re-enumerates it
  (clears a stuck mouse button).
- `abs X Y` — absolute pointer position, 0..32767 per axis (the `flash.sh --abs` build only, which enumerates
  as an absolute-coordinate mouse; iOS puts the AssistiveTouch pointer where the report says). In that build
  `move DX DY` is emulated as one report of 45 absolute units per unit (about 1 px per unit on the mirror) and
  `home` goes to (0, 0).
- `trace DT_MS x1 y1 x2 y2 ...` — one absolute report per point, DT_MS (0-50) apart; about 40 points fit one
  command. Replies `ok <points>`. For strokes with the button held (`press` before, `release` after).
When the link drops, the board releases all buttons/keys (same as before).

## status (JSON, one notify per second, must fit one MTU; keep short keys)
```json
{"up":123456,"rst":"power-on","heap":81234,"psram":6200000,"usb":"ok|asleep|off",
 "net":"ap|sta|none","ip":"192.168.4.1","sta":1,
 "ap":"idle|connected|streaming","w":480,"h":1040,"kbps":180,"fps":4.8,"idr":2000,"gop":42000,
 "ble":{"mtu":247,"phy":2,"kbps":350,"drop":0,"rdrop":0,"n":1}}
```
`kbps` = AirPlay video bitrate in, `ble.kbps` = video bytes out over BLE, `idr` = ms between IDRs, `gop` = bytes buffered since last IDR.
`rst` = reset reason of the current boot (`power-on|brownout|watchdog|crash|software|usb|other`), `up` = ms since boot.
`ble.drop` = video notifies given up, `ble.rdrop` = replies that never reached a subscribed peer (should stay 0),
`ble.n` = connected centrals.
`hid.fail` = HID reports that never went out (release not confirmed), `hid.rel` = release retries that were
needed, `ptr` = `rel` (relative mouse) or `abs` (absolute build), `logdrop` = board log lines dropped by the
rate limit.

Send priority: replies and status are queued and always go out before further video; video keeps at most
~8 ACL packets (~2 KB) in the controller, so a reply waits behind at most that much video. In `stream on`,
a P-frame more than 800 ms old is skipped and the stream resumes at the next IDR (a slow link degrades to
IDR-only instead of building latency).

## video
Each notify: `[seq u16 LE][flags u8][payload]`, payload up to MTU-3-3 bytes.
flags: bit0 START of access unit, bit1 END of access unit, bit2 IDR, bit3 CONFIG (payload is SPS+PPS),
bit4 BENCH (filler, ignore), bit5 SNAP (part of a snap reply).
Payload is Annex-B (`00 00 00 01` start codes). On a START chunk the first 4 payload bytes are a u32 LE
board timestamp (ms) for latency stats, followed by the Annex-B data.
`seq` increments per notify; the app counts gaps as drops and waits for the next IDR/CONFIG after a gap.

GOP buffer overflow (4 MB, or 8192 AUs): iOS sends one IDR per mirroring session, so a long session outgrows
the buffer. If a `stream on` client has received the whole buffer so far, the board drops only the AUs that
client already has and keeps the rest (a *continuation*): that client gets CONFIG again and then the remaining
and all new AUs, and sees no gap. A continuation cannot be decoded from its start, so `snap` and a client that
starts `stream on` later get nothing until the next IDR (restart Screen Mirroring on the phone). Without a
caught-up streaming client the whole buffer is dropped until the next IDR, as before.

## net (JSON)
Read: `{"mode":"ap","ssid":"ESP32Agent-1A2B","pass":"<random 12 chars>","ip":"192.168.4.1"}`
(SoftAP credentials are generated once at first boot and kept in NVS).
Write `{"mode":"ap"}` or `{"mode":"sta","ssid":"...","pass":"..."}` — stored in NVS, board restarts Wi-Fi.

## log (reply characteristic)
A board built with `flash.sh --log` sends `log <text>` lines on `reply` (no request id): HID delivery
problems, USB mount/suspend/resume, AirPlay session steps, GOP overflow/compaction, BLE drops. At most 20 per
second; a line that follows dropped ones ends with ` (+N dropped)`.

## AirPlay
Receiver name `ESP32 Agent`, mirroring only (no audio), H.264 only, resolution/fps from NVS.

## Deviations (firmware notes)
- `status`: if the negotiated MTU is too small for the full JSON (MTU-3 < ~255 bytes), the notify carries a
  reduced object `{"up","rst","usb","net","ap","ble":{"mtu"}}`; a `status` read always returns the last full JSON.
- `sta` in `status` = number of stations on the board's hotspot (mode `ap`), or 1/0 = connected (mode `sta`).
- `net` read in `sta` mode returns the station network's `ssid`/`pass`.
- `snap` while mirroring has stopped returns the last buffered GOP (check `"ap"` in status); `snap` without
  any video replies `err no video ...`.
