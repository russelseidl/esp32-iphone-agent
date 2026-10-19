# Pointer Finding, Grid Calibration and Precise Taps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Taps land within 1% of their target anywhere on the screen, calibrated and verified by the server from the mirror itself, with no hand-read pointer positions.

**Architecture:** Spec section 2 of `docs/superpowers/specs/2026-10-19-measured-navigation-design.md`. Two pure modules carry the logic and the tests: `mac/pointer.py` finds the AssistiveTouch pointer by differencing a settled frame against one taken after a nudge of known direction (the "before" blob is on the side opposite the nudge); `mac/calibration.py` maps mouse units to screen fractions per axis with a monotone piecewise-linear map fitted from a 3x3 grid plus the homed position, learning from measured misses. The server (`mac/esp32_agent_mcp.py`) wires them into `find_pointer`, `calibrate_auto` and `tap(precise=True)`, and feeds verified landings to the stuck-button detector from plan 1.

**Tech Stack:** Python 3.13, Pillow (new; wheels for macOS arm64), pytest via `uv run --with`. No firmware change.

---

## File map

| File | Responsibility | Why new / rung checked |
|---|---|---|
| `mac/pointer.py` (new) | grayscale decode, settle test, changed-pixel blob, old/new split by nudge direction | nothing in the repo; stdlib has no image decode; Pillow gives pixels, the logic is ours |
| `mac/calibration.py` (new) | `Calibration`: affine or piecewise map, inverse, grid fit, learning from landings, (de)serialisation | replaces the affine-only code inside `Phone` so it can be tested without BLE |
| `mac/esp32_agent_mcp.py` (modify) | Pillow dep, `Link.find_pointer`, `Phone` uses `Calibration`, tools `find_pointer`, `calibrate_auto`, `tap(precise)`, landing-based stuck trigger | spec 2 |
| `mac/install_agent.sh` (modify) | install Pillow into `.deps` | spec 2 dependency |
| `mac/tests/test_pointer.py`, `mac/tests/test_calibration.py` (new) | unit tests on synthetic frames and points | spec testing |
| `mac/README.md`, `docs/superpowers/plans/...` (modify) | tool docs, field-check record | spec |

Conventions as in plan 1. Coordinates: "units" are mouse units from the homed top-left corner; "frac" is 0..1 of the screen; tools speak 0-1000.

---

### Task 1: `mac/pointer.py` with synthetic-frame tests

**Files:**
- Create: `mac/pointer.py`
- Create: `mac/tests/test_pointer.py`

- [ ] **Step 1: Write the failing tests**

```python
import io

from PIL import Image, ImageDraw

from pointer import Frame, changed_fraction, locate


def frame(w=240, h=520, pointer=None, bg=(40, 90, 160), extra=None) -> Frame:
    """A synthetic mirror frame: flat background, optional grey pointer disc, optional extra rectangle."""
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    if pointer:
        x, y = pointer
        d.ellipse((x - 7, y - 7, x + 7, y + 7), fill=(120, 120, 120))
    if extra:
        d.rectangle(extra, fill=(250, 250, 250))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return Frame.from_jpeg(buf.getvalue(), scale=1)


def test_identical_frames_are_settled():
    a, b = frame(pointer=(100, 200)), frame(pointer=(100, 200))
    assert changed_fraction(a, b) < 0.002


def test_moved_pointer_is_not_settled_but_small():
    a, b = frame(pointer=(100, 200)), frame(pointer=(120, 200))
    assert 0.0005 < changed_fraction(a, b) < 0.01


def test_locate_returns_old_and_new_for_a_right_nudge():
    a, b = frame(pointer=(100, 200)), frame(pointer=(124, 200))
    found = locate(a, b, nudge_sign=+1)
    assert found is not None
    (ox, oy), (nx, ny) = found
    assert abs(ox - 100 / 240) < 0.02 and abs(oy - 200 / 520) < 0.02
    assert abs(nx - 124 / 240) < 0.02 and abs(ny - 200 / 520) < 0.02


def test_locate_handles_a_left_nudge():
    a, b = frame(pointer=(200, 300)), frame(pointer=(176, 300))
    (ox, oy), (nx, ny) = locate(a, b, nudge_sign=-1)
    assert abs(ox - 200 / 240) < 0.02 and abs(nx - 176 / 240) < 0.02


def test_locate_rejects_a_screen_that_changed_elsewhere():
    a = frame(pointer=(100, 200))
    b = frame(pointer=(124, 200), extra=(0, 400, 240, 520))  # a big UI change
    assert locate(a, b, nudge_sign=+1) is None


def test_locate_rejects_no_change():
    a, b = frame(pointer=(100, 200)), frame(pointer=(100, 200))
    assert locate(a, b, nudge_sign=+1) is None


def test_scale_keeps_fractions():
    a = Frame.from_jpeg(frame(pointer=(100, 200)).jpeg, scale=2)
    b = Frame.from_jpeg(frame(pointer=(124, 200)).jpeg, scale=2)
    (ox, oy), _ = locate(a, b, nudge_sign=+1)
    assert abs(ox - 100 / 240) < 0.03 and abs(oy - 200 / 520) < 0.03
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests/test_pointer.py -q`
Expected: `ModuleNotFoundError: No module named 'pointer'`.

- [ ] **Step 3: Write `mac/pointer.py`**

```python
"""Find the AssistiveTouch pointer in mirror frames by differencing.

The pointer is a small translucent grey disc. Between a settled frame and one taken after a nudge of known
direction, the only pixels that change are the pointer's old and new footprints; the old one lies on the side
opposite the nudge. Everything is pure Python over Pillow pixel access: frames are small (a few hundred px
wide after scaling) and this runs once per precise tap.
"""
import io
from dataclasses import dataclass

from PIL import Image

DIFF_THRESHOLD = 28     # grey levels; JPEG noise stays well under this on a still screen
MIN_PIXELS = 12         # smaller changed sets are noise
MAX_PIXELS_FRAC = 0.012  # larger changed sets mean the screen itself changed
MAX_SPAN_FRAC = 0.25    # the two footprints of one nudge never span more than this of the width/height


@dataclass
class Frame:
    jpeg: bytes
    w: int
    h: int
    gray: bytes  # row-major 8-bit

    @classmethod
    def from_jpeg(cls, jpeg: bytes, scale: int = 2) -> "Frame":
        im = Image.open(io.BytesIO(jpeg)).convert("L")
        if scale > 1:
            im = im.resize((max(1, im.width // scale), max(1, im.height // scale)), Image.BILINEAR)
        return cls(jpeg, im.width, im.height, im.tobytes())


def _changed(a: Frame, b: Frame, threshold: int = DIFF_THRESHOLD) -> list[tuple[int, int]]:
    if (a.w, a.h) != (b.w, b.h):
        raise ValueError("frames differ in size")
    ga, gb, w = a.gray, b.gray, a.w
    return [(i % w, i // w) for i in range(len(ga)) if abs(ga[i] - gb[i]) > threshold]


def changed_fraction(a: Frame, b: Frame) -> float:
    """Share of pixels that differ: ~0 for a still screen."""
    return len(_changed(a, b)) / (a.w * a.h)


def locate(a: Frame, b: Frame, nudge_sign: int) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Pointer centre before (in a) and after (in b) a horizontal nudge in direction nudge_sign (+1 right).
    Both as screen fractions (0..1). None if the change is not a lone small blob pair."""
    pts = _changed(a, b)
    n = len(pts)
    if n < MIN_PIXELS or n > MAX_PIXELS_FRAC * a.w * a.h:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if max(xs) - min(xs) > MAX_SPAN_FRAC * a.w or max(ys) - min(ys) > MAX_SPAN_FRAC * a.h:
        return None
    mid = (min(xs) + max(xs)) / 2
    left = [p for p in pts if p[0] <= mid]
    right = [p for p in pts if p[0] > mid]
    if not left or not right:
        return None
    old, new = (left, right) if nudge_sign > 0 else (right, left)

    def centre(ps):
        return (sum(p[0] for p in ps) / len(ps) + 0.5) / a.w, (sum(p[1] for p in ps) / len(ps) + 0.5) / a.h

    return centre(old), centre(new)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests/test_pointer.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add mac/pointer.py mac/tests/test_pointer.py
git commit -m "mac: pointer finder by nudge differencing, with synthetic-frame tests" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `mac/calibration.py` with tests

**Files:**
- Create: `mac/calibration.py`
- Create: `mac/tests/test_calibration.py`

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from calibration import Calibration


def test_affine_roundtrip_from_legacy_dict():
    c = Calibration.from_dict({"ax": 0.00372, "bx": 0.04571, "ay": 0.00162, "by": 0.02429})
    assert c.model == "affine"
    assert c.units("x", 0.609) == 151 and c.units("y", 0.921) == 554
    assert abs(c.frac("x", 151) - 0.6075) < 0.002


def test_grid_fit_builds_a_monotone_piecewise_map():
    c = Calibration()
    # x: homed at 0.045, then 60->0.27, 130->0.52, 220->0.82 (sub-linear near the end, as on the real phone)
    c.fit_grid("x", [(0, 0.045), (60, 0.27), (130, 0.52), (220, 0.82)], res=(718, 1560))
    assert c.model == "pwl" and c.res == (718, 1560)
    assert c.units("x", 0.045) == 0 and c.units("x", 0.52) == 130
    assert c.units("x", 0.395) == 95            # midway in the 60..130 segment
    assert c.units("x", 0.67) == 175            # midway in the 130..220 segment
    assert c.units("x", 0.95) == 259            # extrapolated past the last knot on its slope
    assert c.units("x", 0.0) == 0                # never negative


def test_fit_grid_rejects_non_monotone_points():
    c = Calibration()
    with pytest.raises(ValueError):
        c.fit_grid("x", [(0, 0.05), (60, 0.5), (130, 0.4)], res=(718, 1560))


def test_learn_moves_the_nearest_knot_toward_measured_landings():
    c = Calibration()
    c.fit_grid("x", [(0, 0.045), (60, 0.27), (130, 0.52), (220, 0.82)], res=(718, 1560))
    for _ in range(6):
        c.learn("x", 130, 0.50)  # taps aimed at knot 130 keep landing 2% short
    assert abs(c.frac("x", 130) - 0.51) < 0.012  # knot blends toward the measured value, not all the way at once


def test_to_dict_from_dict_roundtrip_and_invalidation_on_resolution_change():
    c = Calibration()
    c.fit_grid("x", [(0, 0.045), (60, 0.27), (130, 0.52), (220, 0.82)], res=(718, 1560))
    c.fit_grid("y", [(0, 0.02), (100, 0.19), (300, 0.5), (520, 0.84)], res=(718, 1560))
    d = Calibration.from_dict(c.to_dict())
    assert d.units("y", 0.5) == 300 and d.valid_for((718, 1560)) and not d.valid_for((480, 1040))


def test_needs_both_axes_before_use():
    c = Calibration()
    c.fit_grid("x", [(0, 0.045), (60, 0.27), (130, 0.52)], res=(718, 1560))
    assert not c.ready
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests/test_calibration.py -q`
Expected: `ModuleNotFoundError: No module named 'calibration'`.

- [ ] **Step 3: Write `mac/calibration.py`**

```python
"""Mouse units -> screen fraction per axis, and back.

Two models: `affine` (frac = a*units + b, the old two-point fit, kept as a fallback) and `pwl` (monotone
piecewise-linear through measured knots: the homed position and a grid of stops, which captures iOS pointer
acceleration being sub-linear far from the start). Knots learn from verified tap landings: each landing near a
knot blends that knot's fraction toward the measurement (one fifth per landing), so accuracy improves in use
without one bad reading throwing the map off.
"""
from bisect import bisect_right

LEARN_RATE = 0.2
LEARN_REACH = 0.25  # a landing updates the knot whose units are within this share of the knot spacing


class Calibration:
    def __init__(self):
        self.model: str | None = None
        self.res: tuple[int, int] | None = None
        self.affine: dict[str, float] = {}
        self.knots: dict[str, list[tuple[float, float]]] = {}  # axis -> [(units, frac)...] sorted by units

    # ---- construction / persistence
    @classmethod
    def from_dict(cls, d: dict | None) -> "Calibration":
        c = cls()
        if not d:
            return c
        if d.get("model") == "pwl":
            c.model = "pwl"
            c.res = tuple(d["res"]) if d.get("res") else None
            c.knots = {ax: [tuple(p) for p in d.get(ax, [])] for ax in ("x", "y") if d.get(ax)}
        elif "ax" in d:
            c.model = "affine"
            c.affine = {k: float(d[k]) for k in ("ax", "bx", "ay", "by")}
        elif "w" in d:
            c.model = "affine"
            c.affine = {"ax": 1 / d["w"], "bx": 0.0, "ay": 1 / d["h"], "by": 0.0}
        return c

    def to_dict(self) -> dict:
        if self.model == "pwl":
            return {"model": "pwl", "res": list(self.res) if self.res else None,
                    **{ax: [list(p) for p in pts] for ax, pts in self.knots.items()}}
        return dict(self.affine)

    @property
    def ready(self) -> bool:
        if self.model == "affine":
            return len(self.affine) == 4
        return self.model == "pwl" and all(len(self.knots.get(ax, [])) >= 2 for ax in ("x", "y"))

    def valid_for(self, res: tuple[int, int]) -> bool:
        return self.model == "affine" or (self.ready and self.res == tuple(res))

    def span(self) -> dict | None:
        """Mouse units across the whole screen, for display."""
        if not self.ready:
            return None
        return {"w": self.units("x", 1.0), "h": self.units("y", 1.0)}

    # ---- fitting
    def fit_grid(self, axis: str, points: list[tuple[float, float]], res: tuple[int, int]) -> None:
        pts = sorted((float(u), float(f)) for u, f in points)
        if len(pts) < 2:
            raise ValueError("need at least 2 points")
        for (u0, f0), (u1, f1) in zip(pts, pts[1:]):
            if u1 <= u0 or f1 <= f0:
                raise ValueError(f"{axis} points must increase in both units and fraction: {pts}")
        if self.model != "pwl" or self.res != tuple(res):
            self.knots = {}
        self.model, self.res = "pwl", tuple(res)
        self.knots[axis] = pts

    def learn(self, axis: str, units: float, frac: float) -> None:
        """A verified landing: `units` were sent from home and the pointer was seen at `frac`."""
        if self.model != "pwl" or axis not in self.knots:
            return
        pts = self.knots[axis]
        i = min(range(len(pts)), key=lambda k: abs(pts[k][0] - units))
        spacing = (pts[-1][0] - pts[0][0]) / max(1, len(pts) - 1)
        if abs(pts[i][0] - units) > LEARN_REACH * spacing:
            return
        u, f = pts[i]
        new_f = f + LEARN_RATE * (frac - f)
        lo = pts[i - 1][1] if i > 0 else -1.0
        hi = pts[i + 1][1] if i + 1 < len(pts) else 2.0
        if lo < new_f < hi:  # keep the map monotone
            pts[i] = (u, new_f)

    # ---- mapping
    def frac(self, axis: str, units: float) -> float:
        if self.model == "affine":
            a, b = self.affine["a" + axis], self.affine["b" + axis]
            return a * units + b
        pts = self.knots[axis]
        us = [p[0] for p in pts]
        i = max(1, min(bisect_right(us, units), len(pts) - 1))
        (u0, f0), (u1, f1) = pts[i - 1], pts[i]
        return f0 + (units - u0) * (f1 - f0) / (u1 - u0)

    def units(self, axis: str, frac: float) -> int:
        if self.model == "affine":
            a, b = self.affine["a" + axis], self.affine["b" + axis]
            return max(0, round((frac - b) / a))
        pts = self.knots[axis]
        fs = [p[1] for p in pts]
        i = max(1, min(bisect_right(fs, frac), len(pts) - 1))
        (u0, f0), (u1, f1) = pts[i - 1], pts[i]
        return max(0, round(u0 + (frac - f0) * (u1 - u0) / (f1 - f0)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests -q`
Expected: `21 passed` (8 from plan 1, 7 pointer, 6 calibration).

- [ ] **Step 5: Commit**

```bash
git add mac/calibration.py mac/tests/test_calibration.py
git commit -m "mac: calibration map (affine fallback, piecewise grid fit, learning) with tests" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Server wiring: Pillow, find_pointer, calibrate_auto, precise taps

**Files:**
- Modify: `mac/esp32_agent_mcp.py`
- Modify: `mac/install_agent.sh`

- [ ] **Step 1: Dependencies**

In `mac/esp32_agent_mcp.py` change the script header line to `# dependencies = ["mcp>=2", "bleak>=0.22", "pillow>=10"]`. In `mac/install_agent.sh` change the `uv pip install` line to end with `"mcp>=2" "bleak>=0.22" "pillow>=10"`. Add to the imports:

```python
from calibration import Calibration
from pointer import Frame, changed_fraction, locate
```

- [ ] **Step 2: `Link.find_pointer`** (after `frame()` in `Link`):

```python
    NUDGE = 12          # mouse units the pointer is wiggled by to find it
    SETTLE_S = 1.5      # max wait for a still screen
    FRESH_S = 1.2       # max wait for a frame after the nudge

    async def _settled_frame(self) -> Frame:
        """A frame taken when two consecutive frames barely differ (the screen is still)."""
        deadline = time.monotonic() + self.SETTLE_S
        prev = Frame.from_jpeg((await self.frame())[0])
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            cur = Frame.from_jpeg((await self.frame())[0])
            if changed_fraction(prev, cur) < 0.002:
                return cur
            prev = cur
        return prev

    async def find_pointer(self, near_right_edge: bool = False) -> tuple[float, float] | None:
        """Where the pointer is now, as screen fractions; None if it could not be told apart from the screen.
        Nudges right (left near the right edge) and back; the pointer ends where it started."""
        sign = -1 if near_right_edge else 1
        before = await self._settled_frame()
        await self._cmd(f"move {sign * self.NUDGE} 0")
        t_sent = time.monotonic()
        after = None
        deadline = time.monotonic() + self.FRESH_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            if self.decoder.at > t_sent + 0.2:
                after = Frame.from_jpeg(self.decoder.png)
                break
        await self._cmd(f"move {-sign * self.NUDGE} 0")
        if after is None:
            TELEMETRY.event("pointer", found=False, why="no fresh frame after the nudge")
            return None
        found = locate(before, after, sign)
        TELEMETRY.event("pointer", found=found is not None, at=found[0] if found else None)
        return found[0] if found else None
```

- [ ] **Step 3: `Phone` uses `Calibration`**

Replace the calibration parts of `Phone.__init__` (from `saved = ...` through `self._pos = None`) with:

```python
        self.cal = Calibration.from_dict(json.loads(CAL_FILE.read_text()) if CAL_FILE.exists() else None)
        self._pos = None
```

Replace `calibrated`, `span`, `save`, `fit`, `_units`, `_dx`, `_dy` with:

```python
    @property
    def calibrated(self) -> bool:
        return self.cal.ready

    @property
    def span(self) -> dict | None:
        return self.cal.span()

    def save(self):
        HOME.mkdir(parents=True, exist_ok=True)
        CAL_FILE.write_text(json.dumps(self.cal.to_dict()))

    def fit(self, points):
        """Two-point affine fallback: [[units_x, units_y, screen_x, screen_y], ...] (screen 0-1000)."""
        if len(points) < 2:
            raise RuntimeError("need at least 2 points")
        cal = {}
        for ui, si, a, b in ((0, 2, "ax", "bx"), (1, 3, "ay", "by")):
            us = [float(pt[ui]) for pt in points]
            fs = [float(pt[si]) / 1000 for pt in points]
            mu, mf = sum(us) / len(us), sum(fs) / len(fs)
            var = sum((u - mu) ** 2 for u in us)
            if var < 1:
                raise RuntimeError("the points must differ on both axes")
            cal[a] = sum((u - mu) * (f - mf) for u, f in zip(us, fs)) / var
            cal[b] = mf - cal[a] * mu
            if cal[a] <= 0:
                raise RuntimeError("calibration points are inconsistent (negative scale)")
        self.cal = Calibration.from_dict(cal)
        self.save()
        self._pos = None
        return cal

    def _dx(self, x: float) -> int:
        return self.cal.units("x", min(max(x, 0), 1000) / 1000)

    def _dy(self, y: float) -> int:
        return self.cal.units("y", min(max(y, 0), 1000) / 1000)
```

In `set_calibration` replace the body's first line with `phone.cal = Calibration.from_dict({"ax": 1 / span_w, "bx": 0.0, "ay": 1 / span_h, "by": 0.0})`.

- [ ] **Step 4: Grid calibration and precise tap in `Phone`** (after `point`):

```python
    GRID = (0.15, 0.5, 0.85)  # screen fractions the grid aims at (from the current map, or a default span)

    async def calibrate_auto(self) -> dict:
        """Home, visit a 3x3 grid, find the pointer at each stop, fit the piecewise map for both axes."""
        res = (int(self.link.status.get("w", 480)), int(self.link.status.get("h", 1040)))
        guess = self.cal if self.cal.ready else Calibration.from_dict({"w": 269, "h": 618})
        await self.reset()
        home = await self.link.find_pointer()
        if home is None:
            raise RuntimeError("pointer not found after homing: is AssistiveTouch on and the screen still?")
        xs = [guess.units("x", f) for f in self.GRID]
        ys = [guess.units("y", f) for f in self.GRID]
        seen_x: dict[int, list[float]] = {u: [] for u in xs}
        seen_y: dict[int, list[float]] = {u: [] for u in ys}
        missed = 0
        for uy in ys:
            for ux in xs:
                await self._move_to(ux, uy, rehome=False)
                found = await self.link.find_pointer(near_right_edge=ux == xs[-1])
                if found is None:
                    missed += 1
                    continue
                seen_x[ux].append(found[0])
                seen_y[uy].append(found[1])
        px = [(0, home[0])] + [(u, sum(v) / len(v)) for u, v in seen_x.items() if v]
        py = [(0, home[1])] + [(u, sum(v) / len(v)) for u, v in seen_y.items() if v]
        if len(px) < 3 or len(py) < 3:
            raise RuntimeError(f"only {9 - missed} of 9 grid points found; keep the screen still and retry")
        self.cal = Calibration()
        self.cal.fit_grid("x", px, res)
        self.cal.fit_grid("y", py, res)
        self.save()
        await self.reset()
        TELEMETRY.event("calibrate", res=res, missed=missed, x=px, y=py)
        return {"res": res, "grid_points_found": 9 - missed, "x": px, "y": py, "span": self.span}

    async def tap(self, x, y, precise: bool = False):
        await self.point(x, y)
        if precise:
            found = await self.link.find_pointer(near_right_edge=x > 900)
            if found is not None:
                fx, fy = found[0] * 1000, found[1] * 1000
                miss = ((fx - x) ** 2 + (fy - y) ** 2) ** 0.5
                TELEMETRY.event("aim", aimed=[x, y], landed=[round(fx), round(fy)], miss=round(miss, 1))
                self.cal.learn("x", self._pos[0], found[0])
                self.cal.learn("y", self._pos[1], found[1])
                self.save()
                if self.link.stuck.landing((x, y), (fx, fy)) and self.link._recovering is None:
                    self.link._recovering = asyncio.get_running_loop().create_task(self.link.usb_reset("taps landing far off"))
                    raise RuntimeError("taps are landing far from their aim; resetting USB, try again in a few seconds")
                if miss > 8:
                    dx = self._dx(x) - self._dx(fx)
                    dy = self._dy(y) - self._dy(fy)
                    await self.link.cmd(f"move {dx} {dy}")
                    self._pos = (self._pos[0] + dx, self._pos[1] + dy)
        await self.link.cmd("click")
        await asyncio.sleep(TAP_GAP)
```

(The old `tap` body is replaced by this one.)

- [ ] **Step 5: Tools**

Replace the `tap` tool with:

```python
@mcp.tool()
async def tap(x: int, y: int, precise: bool = False) -> str:
    """Tap the screen at x, y (0-1000, origin top-left). Aim at the visual center of the element. precise=true
    finds the pointer on screen first and corrects the miss before clicking (about 1 s slower): use it for
    small or tightly packed targets such as tab bars, list rows, toggles, icons under 60 units."""
    await phone.tap(x, y, precise)
    return _ok(f"tapped {x},{y}" + (" (precise)" if precise else ""))
```

Add after `calibrate_fit`:

```python
@mcp.tool()
async def calibrate_auto() -> str:
    """Automatic pointer calibration: homes the pointer, visits a 3x3 grid and finds the pointer at each stop
    by nudging it and differencing frames, then fits a piecewise map per axis. Needs a still screen (the Home
    Screen is ideal) and takes about 30 s. Re-run after changing the mirror resolution or pace."""
    return json.dumps(await phone.calibrate_auto())


@mcp.tool()
async def find_pointer() -> str:
    """Where the AssistiveTouch pointer is right now (0-1000), found by nudging it 12 units and back and
    differencing two frames. Returns null if the screen was moving or the pointer could not be seen."""
    found = await link.find_pointer()
    return json.dumps(None if found is None else [round(found[0] * 1000), round(found[1] * 1000)])
```

In the `status` tool, add `"calibration_model": phone.cal.model` next to `"span"`.

- [ ] **Step 6: Verify**

Run: `cd mac && ./install_agent.sh && python3 -m py_compile esp32_agent_mcp.py && uv run --with pytest --with pillow --python 3.13 pytest tests -q`
Expected: installer prints `running: com.esp32agent.mcp ...`; compiles; `21 passed`. `grep -c "find_pointer\|calibrate_auto" esp32_agent_mcp.py` ≥ 8.

- [ ] **Step 7: Commit**

```bash
git add mac/esp32_agent_mcp.py mac/install_agent.sh
git commit -m "mac: find_pointer, calibrate_auto and precise taps on the piecewise calibration" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Field check on the phone

**Files:** none (operations), then the record appended to this plan.

- [ ] **Step 1: Calibrate** — phone unlocked on the Home Screen, mirroring on. Call `calibrate_auto`. Expected: JSON with `grid_points_found` ≥ 7 and monotone `x`/`y` knots; `status` shows `calibration_model: pwl`.

- [ ] **Step 2: Precision on small targets** — call `find_pointer` (expect a position, not null). Then `tap` with `precise=true` on five small targets and screenshot after each: Slack's DMs tab, Slack's search button, the Home Screen Search pill, a Clock tab, the Messages back arrow. Expected: 5 of 5 open the intended control; `diagnostics` `aim` rows show `miss` ≤ 15 units before correction on most and the corrected click landing.

- [ ] **Step 3: Record** — append a "Field check" section to this plan with the knots, the five misses and the pass count, then commit:

```bash
git add docs/superpowers/plans/2026-10-19-pointer-calibration.md
git commit -m "docs: field-check results for pointer calibration" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Docs

**Files:**
- Modify: `mac/README.md`

- [ ] **Step 1: README** — in the Tools table replace the calibration row with:

```markdown
| `calibrate_auto` | automatic 3x3 grid calibration (piecewise map per axis); `calibrate_start`/`calibrate_move`/`calibrate_fit` remain as the manual two-point fallback |
| `find_pointer` | where the pointer is, by nudging it and differencing frames |
```

and change the `tap` row to `| `tap x y [precise]`, ... | gestures, 0-1000 coords; precise finds and corrects the pointer before clicking |`. In the one-time setup step 6, replace the manual instructions with: `6. In Claude, call **`calibrate_auto`** with the phone on its Home Screen (about 30 s). Re-run after changing resolution or pace.`

- [ ] **Step 2: Verify** — `grep -c "calibrate_auto" mac/README.md` → `2`.

- [ ] **Step 3: Commit**

```bash
git add mac/README.md
git commit -m "docs: automatic calibration and precise taps" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review notes

Spec coverage (section 2): finder by nudge differencing with settle check and edge-aware direction (Tasks 1, 3); 3x3 grid calibration with the homed position as a knot, piecewise map, stored with resolution and invalidated on change (`valid_for`; the server re-runs when `res` changes in the bench plan) (Tasks 2, 3); affine fallback kept (Task 3 `fit`); precise taps that verify, correct and learn (Task 3); landing-based stuck trigger (Task 3, using plan 1's detector); paced re-homing already in. Testing bullets: finder on synthetic frames (Task 1), piecewise fit (Task 2), stuck detector (plan 1); field numbers (Task 4).

## Discipline gates

**PLAN_SIMPLICITY:** `pointer.py` — checked stdlib (no image decode) and the legacy `server/hidagent/vision.py` (numpy-based, PIL frames; its idea is reused, its code is not importable here); `calibration.py` — checked the affine code in `Phone` (no piecewise, not testable); Pillow — checked ffmpeg-to-raw (would mean a second decoder process) and the stdlib (none); cut: a connected-components pass (the midpoint split is enough for two blobs) and per-tap refits (knot blending instead).

**PLAN_SURGICAL:** `mac/pointer.py`, `mac/calibration.py`, their tests (spec 2 + testing); `mac/esp32_agent_mcp.py` (spec 2 wiring); `mac/install_agent.sh` (Pillow); `mac/README.md` (docs); this plan (field record).

**PLAN_VERIFY_COVERAGE:** every task has a runnable verify (pytest, py_compile + grep, field check with expected outputs, grep for docs). No PR, so no review task.

**PLAN_TEST_PARITY:** Task 1: too-few / too-many / too-wide / one-sided / scale branches covered. Task 2: affine, pwl interpolation, extrapolation, clamp at 0, non-monotone rejection, learn reach and monotone guard (the "beyond reach" and "would break monotone" branches are exercised indirectly by the blend test staying within bounds — UNTESTED directly because they only refuse an update; the field check covers them), roundtrip, `ready`. Task 3: server glue (asyncio + BLE) — UNTESTED by unit tests, covered by the Task 4 field check.
