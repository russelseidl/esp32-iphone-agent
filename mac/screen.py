"""Coarse screen classification from a decoded frame.

keyboard_visible: the iOS keyboard draws ten keys in its top row with near-equal spacing. Sampling one
scanline through that row gives about twenty brightness transitions at regular intervals; app icons or
text give fewer and irregular ones. Works for both themes because it counts transitions, not shades.
"""
from pointer import Frame

ROW_Y = (0.70, 0.715, 0.73)   # scanlines through the top key row (the keyboard starts near y = 0.67)
MIN_TRANSITIONS = 14
MAX_SPACING_CV = 0.45         # coefficient of variation of the gaps between transitions


def _transitions(frame: Frame, y_frac: float, threshold: int = 22) -> list[int]:
    y = min(frame.h - 1, int(y_frac * frame.h))
    row = frame.gray[y * frame.w:(y + 1) * frame.w]
    xs = []
    x0, x1 = int(frame.w * 0.02), int(frame.w * 0.98)
    for x in range(x0 + 1, x1):
        if abs(row[x] - row[x - 1]) > threshold:
            if not xs or x - xs[-1] > 2:  # one transition per edge
                xs.append(x)
    return xs


def keyboard_visible(frame: Frame) -> bool:
    for y in ROW_Y:
        xs = _transitions(frame, y)
        if len(xs) < MIN_TRANSITIONS:
            continue
        pitches = [b - a for a, b in zip(xs, xs[2:])]  # key pitch: every second edge (key width + gap)
        mean = sum(pitches) / len(pitches)
        cv = (sum((g - mean) ** 2 for g in pitches) / len(pitches)) ** 0.5 / mean
        if cv <= MAX_SPACING_CV:
            return True
    return False
