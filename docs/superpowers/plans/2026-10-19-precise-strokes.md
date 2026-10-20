# Precise Strokes Implementation Plan

> **For agentic workers:** executed inline in the session that wrote it (the user asked for autonomous work to a
> high accuracy bar). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pixel-accurate taps and continuous strokes on the iPhone through the ESP32 board.

**Architecture:** Two routes. A (relative mouse): measure iOS's per-report acceleration curve, invert it in
a path planner with error diffusion, score drawn ink against the intended path. B (absolute mouse): a
firmware build that enumerates as an absolute-coordinate mouse, a linear map fitted with the pointer finder,
taps and strokes as coordinates. The stroke bench scores both the same way.

**Tech Stack:** Python 3.13 (mac/stroke.py, mac/absmap.py, mac/esp32_agent_mcp.py), pytest via uv;
arduino-esp32 3.3.12 USBHIDAbsoluteMouse (TinyUSB ABSMOUSE descriptor); Freeform as the scratch canvas.

---

### Task 1: Measure the acceleration curve (done)

- [x] `probe.py` (scratch): N reports of step s, displacement by the pointer finder. 2026-10-19 17:45, default
  tracking speed: linear in N (2N/N = 2.00 +- 0.01), gain 1.77 px/unit at step 2 to 4.6 at step 64.
  Rows that hit the finder's right-edge limit (463 px) dropped. Saved to `~/.esp32-agent/curve.json`.
- [x] `probe2.py`: mixed-component reports. Magnitude below 2 units moves nothing (dead zone); the
  vector-magnitude gain model holds within 2-5% (finder resolution).
- [x] Research (Apple IOHIDFamily): `velocity = floor(sqrt(dx^2+dy^2))`, gain from a curve of that velocity,
  both axes scaled alike, timing ignored without `HIDPointerReportRate`. Matches the measurements.

### Task 2: Planner and scorer (done)

- [x] `mac/stroke.py`: `Curve` (interpolation, dead zone, inverse), `plan_path` (error diffusion), `score`
  (deviation, p95, coverage, stray, centre-line deviation). Tests: `mac/tests/test_stroke.py` (6).
- [x] Verify: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests -q` -> 50 passed.

### Task 3: Server tools (done)

- [x] `measure_curve`, `draw`, `stroke_bench` in `mac/esp32_agent_mcp.py`; one `move dx dy` per report at
  pace 127; frames under `~/.esp32-agent/strokes/`; `stroke` telemetry rows; the pointer's own closure error
  measured with the finder after each stroke.

### Task 4: Field results, relative route (done)

| shape | centre-line dev (px) | coverage | pointer end miss (px) | reports | s |
|---|---|---|---|---|---|
| line | ~1 | 0.99 | - | 75 | 2.4 |
| square | ~1 | 1.00 | - | 300 | 9.7 |
| diag | ~1 | 0.91 | - | 107 | 3.6 |
| zigzag (2 px/report) | 1.92 | 0.88 | 3.6 | 305 | 20.3 |
| signature (2 px/report) | 1.98 | 0.98 | 4.3 | 199 | 13.2 |

Freeform's pen smoothing rounds corners and shrinks curves slightly; the pointer itself ends 3-4 px from
its predicted position after 200-300 reports (about 1% accumulated model error).

### Task 4b: Floored velocity and planned corrections (done)

- [x] Apple floors the velocity before the curve lookup; measured: a (2,2) report travels 3.53 px per axis
  (the step-2 gain), not the interpolated 3.64. `_travel` now floors the magnitude. y and x gains agree within
  0.5% (no anisotropy).
- [x] Field, 2 px per report: circle centre line 0.5 px (coverage 1.0), zigzag 1.1, signature 1.1, square
  1.0; pointer closure on open paths 0.4-0.7 px (closed shapes end on their own ink, which disturbs the
  finder: not reported there).
- [x] Precise taps correct their miss with planned reports through the curve: 1.8-2.2 units (0.2%) mean
  error, 0 misses; plain 15.9. `rtrace DT dx dy ...` batches reports in the firmware (compiled, flash
  pending); the Mac falls back to one `move` per report on older firmware.
- [x] A finder misread (the clock ticking over at 18:01) had been fed to the map's learn step and pulled it 40
  units; learning now ignores landings over 100 units off, and the finder ignores the status bar.

### Task 4c: Verified taps, sub-pixel finder, signature (done)

- [x] The finder refines its answer at full resolution (`pointer.refine`: centroid of the changed pixels in
  a window around the half-resolution blob, old footprint excluded); precise taps look twice (look, correct
  with planned reports, look again): the second look reads the pointer 0.3-2.4 units (mean 1.3, about 1 px)
  from the aim on all five targets. The ink-based bench error (4 units) is the dab mark's own noise, so the
  bench now also reports the pointer's verified miss.
- [x] Strokes approach the path start with the button up before pressing (no approach hook).
- [x] Signature "Andy", six strokes, 578 reports, 37 s with one `move` per report (rtrace pending flash):
  every stroke ends within 0.5-1.6 px of its target; the remaining wobble is the hand-made control points and
  Freeform's smoothing. Frame: mac/.signature.png (not committed).
- [x] Dead-mouse incident 18:32: moves ignored, buttons alive, right after an agent restart and the 8192-AU
  compaction (which worked: "compacted, 1 AUs kept", no crash). Cure: `usbreset`. The finder now resets USB
  on its own when two consecutive looks see no change at all on a live mirror, and retries.

### Task 5: Absolute pointer firmware (built, not flashed)

- [x] `hardware/flash.sh --abs` -> `-DPOINTER_ABS=1`: `USBHIDAbsoluteMouse`, commands `abs X Y`,
  `trace DT x y ...` (batched points, one report each), `move` emulated at 45 abs units per unit (nudges,
  keep-awake), `home` -> (0,0), status `"ptr":"abs"|"rel"`. Both builds compile (1459 KB / 1458 KB).
- [ ] Flash (`hardware/flash.sh --log --abs`, board on the Mac), board back on the phone, mirroring restarted.

### Task 6: Absolute mode on the Mac (built, not field-checked)

- [x] `mac/absmap.py` (`AbsMap`, least-squares fit, residual) + `tests/test_absmap.py` (4).
- [x] `Phone.abs_mode` from status; `abs_point`, `abs_tap` (precise correction in abs units, dab),
  `abs_drag`, `abs_edge_gesture` (Home, Back, App Switcher), `calibrate_abs` (3x3 grid, affine fit);
  `_draw_px_abs` (path points as absolute reports via `trace`). Every Phone method branches on the mode,
  so all existing tools work unchanged.
- [ ] Field: `calibrate_auto` (-> abs fit, residual), `bench ["aim"]`, `stroke_bench` all shapes; Home,
  Back, App Switcher gestures; `open_app`. Record below.

### Task 7: Pointer finder hardening (done)

- [x] The status bar (top 6%) is ignored: the clock ticking to the next minute was taken for the pointer once
  (aim telemetry 18:01:54, "landed" (127, 40)).

### Task 8: Docs and commit

- [x] Spec: `docs/superpowers/specs/2026-10-19-precise-strokes-design.md`.
- [ ] README: `draw`, `stroke_bench`, `measure_curve`, the `--abs` build; `docs/ble-protocol.md`: `abs`,
  `trace`, `ptr`. Commit.

## Field results, absolute route

Pending the flash.

## Final numbers, relative route, 2026-10-19 18:45

`stroke_bench` all shapes at 2 px per report (mirror px, 718 wide; Freeform pen):

| shape | centre-line dev | p95 ink | coverage | pointer end vs target | vs predicted | reports | s |
|---|---|---|---|---|---|---|---|
| line | 0.46 | 2.0 | 1.000 | 1.7 | 0.2 | 85 | 2.8 |
| diag | 1.03 | 2.8 | 1.000 | 1.6 | 0.2 | 128 | 4.1 |
| square | 0.85 | 3.0 | 0.997 | closed | - | 340 | 10.8 |
| circle | 0.68 | 2.6 | 1.000 | closed | - | 262 | 8.4 |
| zigzag | 1.11 | 2.9 | 0.998 | 1.1 | 0.7 | 305 | 9.8 |
| signature | 1.26 | 2.9 | 1.000 | 1.8 | 1.0 | 200 | 6.5 |

`bench ["aim"]`: plain 17.0 units; precise 3.7 units by the ink mark, **1.0 unit** by the pointer's own
second look (0.1% of the screen, under a mirror pixel). Precise taps take 5.9 s (two looks); performance is
the next pass, starting with the `rtrace` batch (flash pending) and a single look when the first lands.

Remaining: flash `hardware/flash.sh --log --abs` (board on the Mac), then the absolute route's field check
(Task 6): `calibrate_auto`, `bench ["aim"]`, `stroke_bench`, gestures, `open_app`.

## Field results, absolute route, 2026-10-19 18:52-18:58

Flashed `hardware/flash.sh --log --abs`; status `ptr: abs`. iOS places the AssistiveTouch pointer where the
report says: `abs 8192 8192` -> (250, 250), `abs 24575 24575` -> (749, 750), exactly the device range.
`calibrate_auto` (abs fit, 9 of 9): ax 3.075e-5, bx -0.0033, ay 3.053e-5, by 0.0003, residual 0.00115 of the
screen (under a mirror pixel). `open_app freeform` worked through the absolute Home flick, Cmd+Space and typing.

`bench ["aim"]`:

| setting | ink error (units) | pointer verified (units) | latency p50 / p95 (ms) |
|---|---|---|---|
| plain | 4.7 | 1.1 | 977 / 1200 |
| precise | 4.5 | 1.1 | 2938 / 3130 |

A plain absolute tap is as accurate as a corrected one (the first look already reads 0.5 units) and takes
1.0 s instead of 1.9 s (relative plain) or 5.9 s (relative precise). Precise mode is no longer needed here.

`stroke_bench` at 2 px per report (`trace` batches, 4 ms per report):

| shape | centre-line dev | coverage | pointer end vs target | reports | s (relative route) |
|---|---|---|---|---|---|
| line | 0.55 | 1.000 | 1.3 | 150 | 0.8 (2.8) |
| diag | 1.14 | 1.000 | 1.6 | 212 | 1.2 (4.1) |
| square | 0.88 | 0.995 | closed | 600 | 3.6 (10.8) |
| circle | 1.05 | 1.000 | closed | 432 | 2.8 (8.4) |
| zigzag | 1.15 | 0.991 | 2.4 | 558 | 3.1 (9.8) |
| signature | 1.30 | 1.000 | 1.8 | 337 | 1.8 (6.5) |

Same fidelity as the planned relative strokes, 3-5x faster, no acceleration model involved. The residual
~1 px is the mirror's resolution plus Freeform's smoothing.
