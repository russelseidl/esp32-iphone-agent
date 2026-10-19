"""Coarse screen classification from a decoded frame.

keyboard_visible: the iOS keyboard draws ten keys in its top row with near-equal spacing. Sampling one
scanline through that row gives about twenty brightness transitions at regular intervals; app icons or
text give fewer and irregular ones. Works for both themes because it counts transitions, not shades.
"""
from pointer import Frame

KEY_ROWS = (0.70, 0.763)      # scanlines through the first two key rows (iOS keyboard, portrait)
GAP_ROW = 0.731               # the gap between them: almost no edges on a keyboard, many on text
MIN_TRANSITIONS = 14
MAX_SPACING_CV = 0.45         # coefficient of variation of the key pitch
PITCH_RANGE = (0.06, 0.14)    # key pitch as a share of the width (ten keys per row)
MAX_GAP_TRANSITIONS = 6


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


def _key_row(frame: Frame, y: float) -> bool:
    xs = _transitions(frame, y)
    if len(xs) < MIN_TRANSITIONS:
        return False
    pitches = [b - a for a, b in zip(xs, xs[2:])]  # key pitch: every second edge (key width + gap)
    mean = sum(pitches) / len(pitches)
    cv = (sum((g - mean) ** 2 for g in pitches) / len(pitches)) ** 0.5 / mean
    return cv <= MAX_SPACING_CV and PITCH_RANGE[0] <= mean / frame.w <= PITCH_RANGE[1]


def keyboard_visible(frame: Frame) -> bool:
    """Two key rows with ten-key pitch and an empty gap row between them; text lines fail the gap test."""
    return all(_key_row(frame, y) for y in KEY_ROWS) and len(_transitions(frame, GAP_ROW)) <= MAX_GAP_TRANSITIONS
