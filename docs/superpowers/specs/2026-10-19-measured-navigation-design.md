# Measured navigation for the Mac-driven iPhone agent

Date: 2026-10-19. Status: approved design, not yet implemented.

## Context

The Mac MCP server in `mac/` drives an iPhone through the ESP32-S3 board: AirPlay mirror in over BLE, USB
mouse/keyboard out. Primary use is Claude running multi-step tasks on the phone autonomously, with the user
checking in occasionally. Measured today:

- A tap costs two BLE round trips plus a fresh frame: 2-4 s per step, dominated by the ~200 kbps link and the
  mirror's ~1 s latency.
- The pointer map is affine per axis: within ~2% mid-screen, ~5% at the bottom edge, where iOS pointer
  acceleration is non-linear.
- Failures seen: the phone locking (stops mirroring, suspends USB); the board's 4 MB GOP overflowing (fixed by
  the continuation generation); BLE send drops that force a full buffer replay; and a state where the mouse
  looks dead while the keyboard still works, cured only by re-plugging the board. The last matches a dropped
  button-release report: the core's HID path gives up on a report after 100 ms, so iOS keeps the button held,
  every move becomes a drag and taps fire where the drag ends.
- Keyboard shortcuts (Cmd+Space, Cmd+K, Cmd+H) are faster and more reliable than aimed taps, but not every
  app honours them (Muse ignores them; Slack ignores Cmd+H inside a text field).

Decisions taken with the user: optimise for autonomous tasks; choose settings by A/B measurement rather than by
guess; accuracy trials may draw in a scratch Freeform board on the phone.

## Goals

1. Every action, drop and retry is reconstructible afterwards, on the board and the Mac.
2. A tap lands within 1% of its target anywhere on screen, with no hand-read pointer positions.
3. Fewer and more reliable steps per task, preferring inputs that need no aim.
4. Settings and strategies are chosen from measurements that can be re-run at any time.
5. The hardware never needs a hand unless the phone itself is locked, and then it says so.

Out of scope: non-ASCII text entry, landscape, a second BLE link or an L2CAP video channel (these go into the
bench as later experiments: `coex`, PHY), the iOS app.

## Approach

Five pieces, each testable on its own, built in the order 1, 5, 2, 3, 4 (telemetry first so everything after
it is measured; firmware hardening second because it needs one flash; then pointer, navigation, bench).

### 1. Telemetry and logging

Board. A build flag `LOG_BLE=1` (set by `flash.sh --log`) routes the firmware's log lines over the existing
BLE `log` relay (`ble_reply("log ...")`) instead of USB serial: HID report send/wait failures, USB mount,
suspend and resume, AirPlay session start/stop, GOP overflow and compaction, BLE drops and reply drops.
Rate-limited to 20 lines/s with a dropped-lines counter so logging never competes with video. The 1 Hz status
JSON gains `"hid":{"fail":N,"rel":M}`: reports that did not go out, and release retries (section 5).

Mac. The server appends one JSON object per line to `~/.esp32-agent/telemetry.jsonl`: `{"t": epoch,
"kind": ..., ...}` with kinds `cmd` (line, reply, round-trip ms), `frame` (board ts, lag ms, live flag),
`boardlog` (the relayed line), `link` (connect, disconnect, reconnect attempt), `replay` (bytes to go),
`recover` (action taken and result), `trial` (bench, section 4). The existing human-readable log stays.

Tool `diagnostics(n=50)` returns the last n events and the current counters.

### 2. Pointer finding, calibration and aiming

Pointer finder (`find_pointer`). Capture a frame, send `move +N 0`, wait for a fresh frame, capture, send
`move -N 0`. The pointer is the small blob (about 12-30 px at 718 px width) whose pixels changed between the
two frames; everything else is still (the server waits for the screen to settle first: two consecutive frames
nearly identical). Implemented in pure Python over Pillow pixel access on the decoded JPEG. Returns the center
in 0-1000 units, or `None`. The nudge N is 12 units, direction flipped near the right edge.

Grid calibration (`calibrate_auto`). Re-home (paced corner move), then visit a 3x3 grid of mouse-unit targets
spanning the screen, finding the pointer at each. Fit, per axis, a piecewise-linear map with three segments
(units -> screen fraction), monotone, through the measured points. Store with the mirror resolution it was
measured at in `~/.esp32-agent/calibration.json`; invalidate and re-run when `res` changes. The old two-point
affine stays as the fallback when fewer than 6 grid points are found.

Aiming. `tap(x, y, precise=False)`: plain = move then click. Precise = move, find the pointer, correct the
miss, click; about 1 s slower. Every precise tap records (aimed, landed) and the map is refitted from the last
30 such pairs plus the grid, so accuracy improves in use. Targets smaller than 60 units are tapped precise by
the navigator.

Re-homing uses the paced corner move (`move -3000 -3000`), never the firmware's burst `home`.

### 3. Navigation

Composite tools, each verifying its outcome from the next frame and returning `ok` or `no effect`:

- `open_app(name)`: Home (edge drag), Cmd+Space, type name, Enter; verify by a changed frame and, when the
  app is in `apps.yml`, its known signature (title text region).
- `go_home`: edge drag from the pinned bottom edge. `app_switcher`: the same drag with a hold.
- `back`: edge swipe from the pinned left edge.
- `search_in_app(text)`: Cmd+K then Cmd+F per `apps.yml`, else the app's search control by stored geometry.
- `type_text(text)`: wait up to 2 s for the keyboard (a frame region at the bottom becomes the keyboard's
  shade), type, re-read the field via a fresh frame, repair auto-capitalisation of the first character and
  duplicated runs; ASCII only.
- `screenshot` returns the frame plus a state line: `app` (when known), `keyboard` up/down, `live`/`replaying`.

Per-app notes: `~/.esp32-agent/apps.yml`, one entry per app: shortcuts known to work or fail, fixed geometry
(e.g. Slack tab bar y-range, Control Center's Screen Mirroring tile), and a title signature. The navigator
reads it; the bench and the navigator write to it when a shortcut is observed to work or fail.

### 4. Bench harness

Trial = one measured step. Metrics: tap error (distance, 0-1000 units, between intended and landed point),
step latency (command round trip plus time to a fresh frame), frame lag (board ts vs board now), BLE drops and
replays during the trial. Accuracy trials run in a scratch Freeform board: the harness taps a planned grid,
then detects the dots left behind in the next frame (dark blobs on the white canvas) and matches them to the
plan by nearest neighbour.

Axes, swept one at a time from the current best: `res` (718x1560 native, 480x1040, 360x780), `fps` (3, 5,
8), `coex` (bt, balance, wifi), calibration model (affine, piecewise), aiming (plain, precise), pacing (a new
firmware `pace STEP STEP_MS` command; default 10/8, candidates 5/8, 10/4, 20/8). A sweep is 5 trials per
setting, minutes in total.

Output: trials go into the telemetry JSONL; a summary table with the winner and margin per axis is written to
`~/.esp32-agent/bench/<date>.md`. Tool `bench(axes=...)` runs a sweep; the server runs a 5-tap accuracy check
after any recalibration or `res` change. Guardrails: never during a task, never outside Freeform, always ends
with `go_home`.

### 5. Resilience

Firmware (`hardware/hid_bridge`):

- Releases are confirmed: `release`, `releaseAll` and the release half of `click` resend the report until
  the HID layer reports delivery (up to 5 tries, 20 ms apart) and count retries in `hid.rel`; failures count in
  `hid.fail`. `click` = press, release, release. The HID calls go through one thin seam (`hidSend(...)`) so the
  host tests can fake delivery.
- `usbreset`: `tud_disconnect()`, 500 ms, `tud_connect()`; re-enumerates like a re-plug. Replies `ok` before
  disconnecting.
- `pace STEP STEP_MS` sets the move pacing at run time (not stored).
- AirPlay watchdog: if a station is on the hotspot but no mirroring session for 30 s, restart the receiver's
  listening sockets.
- `VIDEO_TIMEOUT_MS` 1500 -> 4000: a BLE stall pauses the stream instead of forcing a full replay.

Mac watchdogs (server):

- Stuck-button detector: two consecutive verified taps landing far (> 150 units) from the tracked position,
  or `hid.fail` rising, triggers `usbreset`, a re-home and a `recover` event.
- BLE loss: reconnect with backoff 1, 2, 4, 8 s, capped at 15 s.
- Mirroring stopped while USB is up: blind macro from `apps.yml` geometry: edge drag from the pinned top-right
  to open Control Center, tap Screen Mirroring, tap ESP32 Agent, verify the stream returns within 10 s; at most
  3 attempts, then report.
- Phone locked (USB suspended and no frames): report plainly, no retries beyond the keep-awake nudge.

### Testing

- Firmware: host tests (`hardware/tests`) for release retry and counters through the `hidSend` seam, for
  `pace` bounds, and the existing GOP tests.
- Server: unit tests (pytest via uv) for the pointer finder on synthetic frames, the piecewise fit, the
  stuck-button detector, the text-entry repair, and telemetry serialisation.
- Integration: the bench harness before and after each change, summaries diffed; a 20-step scripted task
  (open Slack, DM, type, send, home) timed end to end as the headline number.

## Risks

- The pointer finder needs a settled screen; animated backgrounds defeat it. Mitigation: settle check, and
  the navigator falls back to plain taps with a warning.
- `tud_disconnect` may not fully reset iOS's view of the device. Mitigation: measured in the bench; if it does
  not, the fallback is a watchdog-triggered board reboot (USB re-enumerates on boot).
- Freeform's canvas may auto-pan or zoom under drawing. Mitigation: trials use short taps only and re-read the
  canvas scale from two reference dots.
