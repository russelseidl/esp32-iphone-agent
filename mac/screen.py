"""Coarse screen classification from a decoded frame.

keyboard_visible: the iOS keyboard draws ten keys in its top row with near-equal spacing. Sampling one
scanline through that row gives about twenty brightness transitions at regular intervals; app icons or
text give fewer and irregular ones. Works for both themes because it counts transitions, not shades.
"""
from pointer import Frame

KEY_ROWS = (0.70, 0.763)      # scanlines through the first two key rows (iOS keyboard, portrait)
GAP_ROW = 0.731               # the gap between them: nearly flat on a keyboard, busy on text
PITCH_RANGE = (0.07, 0.13)    # key pitch as a share of the width (ten keys per row)
MIN_PERIODICITY = 0.35        # normalised autocorrelation at the key pitch (real keyboards measure 0.44-0.47)
GAP_VARIANCE_RATIO = 0.35     # the gap row's variance relative to the key rows'


def _row(frame: Frame, y_frac: float) -> list[float]:
    y = min(frame.h - 1, int(y_frac * frame.h))
    x0, x1 = int(frame.w * 0.02), int(frame.w * 0.98)
    row = frame.gray[y * frame.w + x0:y * frame.w + x1]
    mean = sum(row) / len(row)
    return [v - mean for v in row]


def _variance(row: list[float]) -> float:
    return sum(v * v for v in row) / len(row)


def _periodicity(row: list[float], w: int) -> tuple[float, float]:
    """Best normalised autocorrelation over key-pitch lags, and that lag as a share of the width."""
    var = _variance(row)
    if var < 1:
        return 0.0, 0.0
    n = len(row)
    best, best_lag = 0.0, 0
    for lag in range(int(PITCH_RANGE[0] * w), int(PITCH_RANGE[1] * w) + 1):
        c = sum(row[i] * row[i + lag] for i in range(n - lag)) / ((n - lag) * var)
        if c > best:
            best, best_lag = c, lag
    return best, best_lag / w


def keyboard_visible(frame: Frame) -> bool:
    """Two key rows whose brightness repeats at the ten-key pitch, with a nearly flat gap row between them.
    Letter glyphs inside keys do not break the periodicity; lines of text have no flat gap row."""
    rows = [_row(frame, y) for y in KEY_ROWS]
    for r in rows:
        corr, _ = _periodicity(r, frame.w)
        if corr < MIN_PERIODICITY:
            return False
    key_var = min(_variance(r) for r in rows)
    return _variance(_row(frame, GAP_ROW)) <= GAP_VARIANCE_RATIO * key_var
