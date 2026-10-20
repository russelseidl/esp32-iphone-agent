"""Measurement side of the A/B bench: dots on a white canvas, per-trial records, statistics and the summary.

Score: a setting wins its axis on tap error when errors differ by more than ERROR_MARGIN units; otherwise on
median latency when that differs by more than LATENCY_MARGIN; otherwise the baseline (first listed) stays.
"""
from dataclasses import asdict, dataclass
from statistics import mean, median

from pointer import Frame, _blobs

CANVAS_WHITE = 0.70     # share of pixels above WHITE_LEVEL for a frame to count as a blank canvas (measured: a blank Freeform board with the markup toolbar and the Dynamic Island is 0.785)
WHITE_LEVEL = 225
DOT_LEVEL = 120
DOT_MIN, DOT_MAX = 6, 900   # blob size bounds in pixels at the frame's scale
ERROR_MARGIN = 3.0
LATENCY_MARGIN = 150


def find_dots(frame: Frame) -> list[tuple[int, int]] | None:
    """Centres (0-1000) of dark dots on a mostly white frame; None if the frame is not a canvas."""
    g = frame.gray
    white = sum(1 for v in g if v > WHITE_LEVEL) / len(g)
    if white < CANVAS_WHITE:
        return None
    pts = [(i % frame.w, i // frame.w) for i, v in enumerate(g) if v < DOT_LEVEL]
    out = []
    for bl in _blobs(pts):
        if DOT_MIN <= len(bl) <= DOT_MAX:
            out.append((round((sum(p[0] for p in bl) / len(bl) + 0.5) * 1000 / frame.w),
                        round((sum(p[1] for p in bl) / len(bl) + 0.5) * 1000 / frame.h)))
    return out


def match_dots(planned: list[tuple[int, int]], found: list[tuple[int, int]], max_dist: float = 120) -> list[float | None]:
    """Error per planned tap (distance to its nearest unused dot) or None if no dot landed within max_dist."""
    free = list(found)
    errors: list[float | None] = []
    for px, py in planned:
        best = None
        for d in free:
            dist = ((d[0] - px) ** 2 + (d[1] - py) ** 2) ** 0.5
            if dist <= max_dist and (best is None or dist < best[0]):
                best = (dist, d)
        if best is None:
            errors.append(None)
        else:
            free.remove(best[1])
            errors.append(round(best[0], 1))
    return errors


@dataclass
class Trial:
    axis: str
    setting: str
    error: float | None       # units; None = the tap left no dot where expected
    latency_ms: float         # command round trip plus wait for a fresh frame
    drops: int = 0            # BLE video drops during the trial
    calibrated: bool = False  # the pointer map was refitted for this setting before the taps (res, pace)
    pointer_err: float | None = None  # precise taps: the pointer's miss on its last look, before the click (units)

    def row(self) -> dict:
        return {"kind": "trial", **asdict(self)}


def summarize(trials: list[Trial]) -> dict:
    out: dict = {}
    for t in trials:
        s = out.setdefault(t.axis, {}).setdefault(t.setting, {"errors": [], "latencies": [], "drops": 0, "misses": 0,
                                                              "calibrated": t.calibrated, "pointer_errs": []})
        if t.pointer_err is not None:
            s["pointer_errs"].append(t.pointer_err)
        if t.error is None:
            s["misses"] += 1
        else:
            s["errors"].append(t.error)
        s["latencies"].append(t.latency_ms)
        s["drops"] += t.drops
    for axis in out.values():
        for s in axis.values():
            s["n"] = len(s["latencies"])
            s["error_mean"] = round(mean(s["errors"]), 1) if s["errors"] else None
            s["pointer_err_mean"] = round(mean(s["pointer_errs"]), 1) if s["pointer_errs"] else None
            del s["pointer_errs"]
            lat = sorted(s["latencies"])
            s["latency_p50"] = median(lat)
            s["latency_p95"] = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
            del s["errors"], s["latencies"]
    return out


def pick_winners(summary: dict) -> dict:
    winners = {}
    for axis, settings in summary.items():
        names = list(settings)
        base = names[0]
        best, by = base, "baseline"
        for name in names[1:]:
            a, b = settings[best], settings[name]
            ea, eb = a["error_mean"], b["error_mean"]
            if eb is None:
                continue
            if ea is None or ea - eb > ERROR_MARGIN:
                best, by = name, "error"
            elif abs(ea - eb) <= ERROR_MARGIN and a["latency_p50"] - b["latency_p50"] > LATENCY_MARGIN:
                best, by = name, "latency"
        winners[axis] = {"winner": best, "by": by, "baseline": base}
    return winners


def to_markdown(summary: dict, winners: dict) -> str:
    lines = ["| axis | setting | n | error mean (units) | pointer err (units) | misses | latency p50 / p95 (ms) | drops | calibrated | result |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for axis, settings in summary.items():
        for name, s in settings.items():
            tag = f"winner ({winners[axis]['by']})" if winners[axis]["winner"] == name else ""
            lines.append(f"| {axis} | {name} | {s['n']} | {s['error_mean']} | {s.get('pointer_err_mean')} | {s['misses']} | "
                         f"{s['latency_p50']:.0f} / {s['latency_p95']:.0f} | {s['drops']} | "
                         f"{'yes' if s.get('calibrated') else 'no'} | {tag} |")
    return "\n".join(lines) + "\n"
