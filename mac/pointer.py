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
