"""Absolute pointer map: HID absolute coordinates (0..32767 per axis) <-> screen fractions (0..1).

With an absolute mouse the pointer goes where the report says, so the map is a straight line per axis:
frac = a * abs + b. It is fitted from a grid of commanded positions read back with the pointer finder,
which also tells how linear the device really is (residual)."""
from __future__ import annotations

import json
from pathlib import Path

ABS_MAX = 32767


class AbsMap:
    def __init__(self, ax: float = 1 / ABS_MAX, bx: float = 0.0, ay: float = 1 / ABS_MAX, by: float = 0.0,
                 residual: float | None = None):
        self.ax, self.bx, self.ay, self.by = ax, bx, ay, by
        self.residual = residual  # mean fit residual in screen fractions (None = not measured)

    @property
    def measured(self) -> bool:
        return self.residual is not None

    def frac(self, axis: str, value: int) -> float:
        a, b = (self.ax, self.bx) if axis == "x" else (self.ay, self.by)
        return a * value + b

    def abs(self, axis: str, frac: float) -> int:
        a, b = (self.ax, self.bx) if axis == "x" else (self.ay, self.by)
        return max(0, min(ABS_MAX, round((frac - b) / a)))

    def to_dict(self) -> dict:
        return {"ax": self.ax, "bx": self.bx, "ay": self.ay, "by": self.by, "residual": self.residual}

    @classmethod
    def from_dict(cls, d: dict | None) -> "AbsMap":
        return cls(**d) if d else cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()))

    @classmethod
    def load(cls, path: Path) -> "AbsMap":
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return cls()


def fit_axis(points: list[tuple[int, float]]) -> tuple[float, float, float]:
    """Least-squares line frac = a * abs + b through (abs, frac) points; returns (a, b, mean |residual|)."""
    if len(points) < 2:
        raise ValueError("need at least 2 points")
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx == 0:
        raise ValueError("points must span different abs values")
    a = sum((p[0] - mx) * (p[1] - my) for p in points) / sxx
    b = my - a * mx
    res = sum(abs(a * p[0] + b - p[1]) for p in points) / n
    return a, b, res


def fit(xs: list[tuple[int, float]], ys: list[tuple[int, float]]) -> AbsMap:
    ax, bx, rx = fit_axis(xs)
    ay, by, ry = fit_axis(ys)
    return AbsMap(ax, bx, ay, by, residual=round((rx + ry) / 2, 5))
