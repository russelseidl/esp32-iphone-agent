# Precise strokes and absolute pointing

Date: 2026-10-19. Status: design written while building (the user asked for autonomous work to a high
accuracy bar, no approval gates); sections record what was measured and decided.

## Goal

Pixel-accurate manipulation of the iPhone screen through the ESP32 board: taps that land where aimed and
continuous strokes (a drawn shape, a signature) that follow the intended path to about a pixel of the
mirror frame (718 x 1560 px for the 1179 x 2556 screen).

## What was measured first (relative mouse, AssistiveTouch pointer)

- iOS scales each HID report by a gain that depends on the report's step size only. Travel is exactly
  linear in the number of reports (40 reports = 2 x 20 reports within a pixel), so there is no per-move
  ramp loss. The report interval does not matter (10/4 and 10/8 pacing land identically). Apple's
  IOHIDFamily source confirms the model: `velocity = floor(sqrt(dx^2 + dy^2))`, gain = curve(velocity)
  applied to both axes, and wall-clock timing ignored for a device without `HIDPointerReportRate`.
- Gain at the default tracking speed: 1.77 px/unit at step 2 rising to about 4.6 at step 64.
- Reports with magnitude below 2 units move nothing (dead zone): (1,0), (0,1), (1,1) x 60 = 0 px.
- Mixed-component reports follow the vector-magnitude model within 2-5%, inside the finder's resolution.
- The minimum tracking speed lowers the gain 4x but buys no accuracy and costs time; default speed kept.

## Approach A: planned relative strokes (built)

`mac/stroke.py`: `Curve` (measured px per report vs step, interpolated, dead zone), `plan_path` (walks the
path at a chosen px per report, picks for each report the integer (dx, dy) whose predicted travel best
matches what is owed, carries the miss forward), `score` (ink vs path: deviation, p95, coverage, stray,
centre-line deviation). Server tools: `measure_curve`, `draw`, `stroke_bench`. One `move dx dy` command per
report at pace 127, so each command is exactly one HID report (about 60 ms each over BLE).

Results (Freeform pen, 300 px shapes): line and square: centre line within about 1 px, coverage 99-100%.
Circle, zigzag, signature: centre line within 2 px, the pointer itself 3-4 px from its predicted end after
200-300 reports (about 1% accumulated model error), and Freeform's own stroke smoothing rounds corners.

Limits: dead reckoning accumulates the residual model error; the app's smoothing is outside our control;
a stroke of 300 reports takes 10-20 s over BLE (a batch command would fix the time, not the drift).

## Approach B: absolute pointing (built and field-checked: the default from now on)

A USB HID mouse collection with absolute X/Y (TinyUSB `TUD_HID_REPORT_DESC_ABSMOUSE`, logical 0..32767,
`USBHIDAbsoluteMouse` in arduino-esp32 3.x) is reported to place the AssistiveTouch pointer directly at
the commanded position on iPhone (fork jinwon-int/esp32-iphone-agent, PR 19: 0.8% mean error, 15-23 ms
latency, portrait only). If it holds on this phone, taps and strokes become coordinates: no acceleration,
no Home move, no dead reckoning, and a stroke is the path's points sent as absolute reports.

Firmware: a compile-time build (`flash.sh --abs`; the core's HID composite allows one mouse report ID, so
the two mouse types cannot coexist): `abs X Y` (0-32767), `trace DT x y ...` (held button, one report per
point, paced), `rtrace` (relative batches), `move` emulated at 45 abs units per unit, status `ptr`.
Mac: `Phone.abs_mode` from status; `calibrate_abs` (a 3x3 grid of absolute positions read back with the
pointer finder; affine fit) behind `calibrate_auto`; every gesture, tap, drag and stroke branches on the
mode, so the tools and benches are unchanged.

Measured 2026-10-19 on this phone (iOS 26): accepted; the map is the device range to 0.1%; plain taps verify
at 1.1 units in 1.0 s; strokes 0.5-1.3 px centre line, 3-5x faster than the planned relative route.
Remaining caveat: portrait only (the iPhone landscape pointer bug).

## Verification

Unit tests for the curve, planner, scorer and the absolute map. Field: `stroke_bench` per shape and mode,
recorded in docs/superpowers/plans/2026-10-19-precise-strokes.md.
