"""Precise strokes: turn a pixel path into integer HID mouse reports that reproduce it under iOS pointer
acceleration, and score what was drawn against what was asked.

Model (measured 2026-10-19): iOS scales each report by a gain that depends only on the report's step size,
not on the time between reports. The gain is tabulated from measurements (`Curve`) and interpolated; the
planner walks the path, picks for every report the integer step whose predicted travel is closest to what
is still owed, and carries the remainder forward (error diffusion), so rounding never accumulates.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field

DEAD_ZONE = 2.0  # reports whose magnitude is below this move the pointer by nothing at all (measured: 60 reports of
                 # (1,0), (0,1) or (1,1) travelled 0 px; (1,2) travels normally)


@dataclass
class Curve:
    """px travelled per report as a function of the report's step magnitude (integer mouse units)."""
    points: list[tuple[int, float]]  # (step, px per report), sorted by step; step >= 1

    def __post_init__(self):
        self.points = sorted((int(s), float(p)) for s, p in self.points)
        if not self.points or self.points[0][0] < 1:
            raise ValueError("curve needs steps >= 1")

    def px(self, step: float) -> float:
        """Pixels one report of `step` units travels (linear interpolation; constant gain beyond the ends;
        nothing below the dead zone)."""
        pts = self.points
        if step < DEAD_ZONE:
            return 0.0
        if step <= pts[0][0]:
            return pts[0][1] * step / pts[0][0]  # through the origin below the first sample
        if step >= pts[-1][0]:
            return pts[-1][1] * step / pts[-1][0]  # constant gain beyond the last sample
        i = bisect_right([s for s, _ in pts], step)
        (s0, p0), (s1, p1) = pts[i - 1], pts[i]
        return p0 + (step - s0) * (p1 - p0) / (s1 - s0)

    def gain(self, step: float) -> float:
        """px per unit at that step size (0 inside the dead zone)."""
        return self.px(step) / step if step > 0 else 0.0

    def step_for(self, px: float, max_step: int = 127) -> int:
        """The integer step whose one-report travel is closest to `px` (0 when px is less than half a step)."""
        best, err = 0, abs(px)
        for s in range(1, max_step + 1):
            e = abs(self.px(s) - px)
            if e < err:
                best, err = s, e
        return best

    def to_dict(self) -> dict:
        return {"points": self.points}

    @classmethod
    def from_dict(cls, d: dict) -> "Curve":
        return cls([tuple(p) for p in d["points"]])


@dataclass
class Plan:
    reports: list[tuple[int, int]] = field(default_factory=list)  # integer (dx, dy) per report
    predicted: list[tuple[float, float]] = field(default_factory=list)  # pointer position after each report

    @property
    def end(self) -> tuple[float, float]:
        return self.predicted[-1] if self.predicted else (0.0, 0.0)


def _travel(curve: Curve, dx: int, dy: int) -> tuple[float, float]:
    """Predicted px travel of one report: the gain is set by the vector magnitude and scales both axes."""
    if dx == 0 and dy == 0:
        return 0.0, 0.0
    mag = math.hypot(dx, dy)
    g = curve.gain(mag)
    return dx * g, dy * g


def plan_path(points: list[tuple[float, float]], curve: Curve, px_per_report: float = 4.0,
              max_step: int = 127, origin: tuple[float, float] | None = None) -> Plan:
    """Reports that trace the polyline `points` (px, in order) at about `px_per_report` per report, starting
    from `origin` (default: the first point). Each report aims at the point `px_per_report` further along
    the path from where the pointer is predicted to be, chooses the integer (dx, dy) whose predicted travel
    best matches, and the miss is carried into the next aim, so the trace stays on the path."""
    if len(points) < 2:
        raise ValueError("a path needs at least two points")
    pos = list(origin or points[0])
    plan = Plan()
    # cumulative arc length along the path
    seg = [math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(seg)
    if total == 0:
        return plan
    cum = [0.0]
    for d in seg:
        cum.append(cum[-1] + d)

    def at(s: float) -> tuple[float, float]:
        s = max(0.0, min(total, s))
        i = max(0, min(len(seg) - 1, bisect_right(cum, s) - 1))
        t = (s - cum[i]) / seg[i] if seg[i] else 0.0
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        return x0 + t * (x1 - x0), y0 + t * (y1 - y0)

    progress = 0.0
    guard = int(total / max(0.5, px_per_report) * 4) + 20
    while guard > 0:
        guard -= 1
        progress = min(total, progress + px_per_report)
        tx, ty = at(progress)
        need = (tx - pos[0], ty - pos[1])
        dx, dy = _best_report(curve, need, max_step)
        if dx == 0 and dy == 0:
            if progress >= total:
                break
            continue
        mx, my = _travel(curve, dx, dy)
        pos[0] += mx
        pos[1] += my
        plan.reports.append((dx, dy))
        plan.predicted.append((pos[0], pos[1]))
        if progress >= total and math.dist(pos, points[-1]) < max(0.5, curve.px(DEAD_ZONE)):
            break
    return plan


def _best_report(curve: Curve, need: tuple[float, float], max_step: int) -> tuple[int, int]:
    """Integer (dx, dy) whose predicted travel is closest to `need`. The magnitude is found from the curve,
    then the integer neighbours of the scaled vector are tried."""
    nx, ny = need
    dist = math.hypot(nx, ny)
    if dist < curve.px(DEAD_ZONE) / 2:
        return 0, 0
    mag = curve.step_for(dist, max_step) or 1
    bx, by = nx / dist * mag, ny / dist * mag
    best, err = (0, 0), dist
    for cx in (math.floor(bx), math.ceil(bx)):
        for cy in (math.floor(by), math.ceil(by)):
            cx2 = max(-max_step, min(max_step, cx))
            cy2 = max(-max_step, min(max_step, cy))
            if math.hypot(cx2, cy2) < DEAD_ZONE:
                continue
            mx, my = _travel(curve, cx2, cy2)
            e = math.hypot(mx - nx, my - ny)
            if e < err:
                best, err = (cx2, cy2), e
    return best


# ---- scoring what was drawn -----------------------------------------------------------------------------

def ink_pixels(gray: list[int], w: int, h: int, level: int = 120, box: tuple[int, int, int, int] | None = None
               ) -> list[tuple[int, int]]:
    """Dark pixels, optionally only inside box (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = box or (0, 0, w, h)
    out = []
    for y in range(max(0, y0), min(h, y1)):
        row = y * w
        for x in range(max(0, x0), min(w, x1)):
            if gray[row + x] < level:
                out.append((x, y))
    return out


def _dist_to_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_to_path(p: tuple[float, float], path: list[tuple[float, float]]) -> float:
    return min(_dist_to_segment(p, path[i], path[i + 1]) for i in range(len(path) - 1))


def score(ink: list[tuple[int, int]], path: list[tuple[float, float]], tol: float = 2.0,
          sample_px: float = 2.0) -> dict:
    """How well the ink matches the path. `deviation`: mean distance of ink pixels to the path (px);
    `p95`: 95th percentile of it; `coverage`: share of path samples (every `sample_px`) with ink within
    `tol`; `stray`: share of ink pixels farther than 3*tol from the path."""
    if not ink:
        return {"deviation": None, "p95": None, "coverage": 0.0, "stray": 0.0, "ink": 0}
    ds = sorted(dist_to_path(p, path) for p in ink)
    n = len(ds)
    covered = 0
    samples = 0
    s = 0.0
    seg = [math.dist(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg)
    while s <= total:
        # point at arc length s
        acc = 0.0
        pt = path[-1]
        for i, d in enumerate(seg):
            if acc + d >= s:
                t = (s - acc) / d if d else 0.0
                pt = (path[i][0] + t * (path[i + 1][0] - path[i][0]), path[i][1] + t * (path[i + 1][1] - path[i][1]))
                break
            acc += d
        samples += 1
        if any(math.hypot(pt[0] - x, pt[1] - y) <= tol for x, y in ink):
            covered += 1
        s += sample_px
    return {"deviation": round(sum(ds) / n, 2), "p95": round(ds[int(0.95 * (n - 1))], 2),
            "coverage": round(covered / max(1, samples), 3), "stray": round(sum(1 for d in ds if d > 3 * tol) / n, 3),
            "ink": n, "center_dev": centerline_deviation(ink, path, sample_px)}


def centerline_deviation(ink: list[tuple[int, int]], path: list[tuple[float, float]], sample_px: float = 2.0,
                         reach: float = 12.0) -> float | None:
    """Mean distance from the path of the ink's local centroid, sampled along the path: the stroke's own
    width cancels out, so this is the error of the stroke's centre line (px)."""
    if not ink or len(path) < 2:
        return None
    seg = [math.dist(path[i], path[i + 1]) for i in range(len(path) - 1)]
    total = sum(seg)
    devs = []
    s = 0.0
    while s <= total:
        acc = 0.0
        pt = path[-1]
        for i, d in enumerate(seg):
            if acc + d >= s:
                t = (s - acc) / d if d else 0.0
                pt = (path[i][0] + t * (path[i + 1][0] - path[i][0]), path[i][1] + t * (path[i + 1][1] - path[i][1]))
                break
            acc += d
        near = [(x, y) for x, y in ink if abs(x - pt[0]) <= reach and abs(y - pt[1]) <= reach
                and math.hypot(x - pt[0], y - pt[1]) <= reach]
        if near:
            c = (sum(x for x, _ in near) / len(near), sum(y for _, y in near) / len(near))
            devs.append(dist_to_path(c, path))
        s += sample_px
    return round(sum(devs) / len(devs), 2) if devs else None
