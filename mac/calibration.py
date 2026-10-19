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
