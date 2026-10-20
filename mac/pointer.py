"""Find the AssistiveTouch pointer in mirror frames by differencing.

The pointer is a small translucent grey disc that iOS hides after a moment of idleness, so a frame taken
before any movement may not show it at all. The server therefore nudges the pointer out, captures, nudges it
back and captures again: between those two frames the only pixels that change are the pointer's two
footprints, the one on the side opposite the second nudge being where it is now. Everything is pure Python over Pillow pixel access: frames are small (a few hundred px
wide after scaling) and this runs once per precise tap.
"""
import io
from dataclasses import dataclass

from PIL import Image, ImageChops

DIFF_THRESHOLD = 28     # grey levels; JPEG noise stays well under this on a still screen
MIN_PIXELS = 12         # smaller changed sets are noise
MAX_PIXELS_FRAC = 0.012  # larger changed sets mean the screen itself changed


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


def _blobs(pts: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """8-connected components of a small set of pixels."""
    todo, out = set(pts), []
    while todo:
        seed = todo.pop()
        blob, stack = [seed], [seed]
        while stack:
            x, y = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (x + dx, y + dy)
                    if q in todo:
                        todo.remove(q)
                        blob.append(q)
                        stack.append(q)
        out.append(blob)
    return out


def _centre(ps, w, h):
    return (sum(p[0] for p in ps) / len(ps) + 0.5) / w, (sum(p[1] for p in ps) / len(ps) + 0.5) / h


STATUS_BAR = 0.06  # top share of the screen ignored by the finder: the clock's minute digits change there


def locate(a: Frame, b: Frame, nudge_sign: int,
           nudge_frac: tuple[float, float] = (0.02, 0.2)) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Pointer centre before (in a) and after (in b) a horizontal nudge in direction nudge_sign (+1 right).
    Both as screen fractions (0..1). The change must contain two similar small blobs on one row, separated
    horizontally by a nudge-sized gap; anything else that changed (a clock hand, a badge) is ignored, and so
    is the status bar (the clock ticking over was once taken for the pointer). None if no such pair exists
    or the screen changed too much."""
    top = int(STATUS_BAR * a.h)
    pts = [p for p in _changed(a, b) if p[1] >= top]
    n = len(pts)
    if n < MIN_PIXELS or n > MAX_PIXELS_FRAC * a.w * a.h:
        return None
    blobs = [bl for bl in _blobs(pts) if MIN_PIXELS // 2 <= len(bl) <= 0.004 * a.w * a.h]
    best = None
    for i, left in enumerate(blobs):
        lc = _centre(left, a.w, a.h)
        for right in blobs[i + 1:]:
            rc = _centre(right, a.w, a.h)
            if lc[0] > rc[0]:
                left, right, lc, rc = right, left, rc, lc
            dx, dy = rc[0] - lc[0], abs(rc[1] - lc[1])
            if not (nudge_frac[0] <= dx <= nudge_frac[1]) or dy > 0.012:
                continue
            score = abs(len(left) - len(right)) / max(len(left), len(right))
            if best is None or score < best[0]:
                best = (score, lc, rc)
    if best is None:
        return None
    _, lc, rc = best
    return (lc, rc) if nudge_sign > 0 else (rc, lc)




def refine(jpeg_a: bytes, jpeg_b: bytes, centre: tuple[float, float], other: tuple[float, float] | None = None,
           win: int = 36, threshold: int = DIFF_THRESHOLD) -> tuple[float, float] | None:
    """Sub-pixel centre of the blob that appears in `jpeg_b` but not in `jpeg_a` near `centre` (fractions),
    read at the frames' full resolution. `locate` works on half-resolution frames (about 2 px quantisation);
    this recomputes the centroid of the changed pixels inside a window of +-`win` full-res px around its
    answer. None when the window holds no change (the blob was not there after all)."""
    a = Image.open(io.BytesIO(jpeg_a)).convert("L")
    b = Image.open(io.BytesIO(jpeg_b)).convert("L")
    w, h = b.size
    cx, cy = centre[0] * w, centre[1] * h
    box = (max(0, int(cx - win)), max(0, int(cy - win)), min(w, int(cx + win)), min(h, int(cy + win)))
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return None
    diff = ImageChops.difference(a.crop(box), b.crop(box)).point(lambda v: 255 if v > threshold else 0)
    pts = [(i % diff.width, i // diff.width) for i, v in enumerate(diff.getdata()) if v]
    if other is not None:  # the blob's previous footprint also changed: keep the pixels nearer the new centre
        ox, oy = other[0] * w - box[0], other[1] * h - box[1]
        ncx, ncy = cx - box[0], cy - box[1]
        pts = [(x, y) for x, y in pts if (x - ncx) ** 2 + (y - ncy) ** 2 < (x - ox) ** 2 + (y - oy) ** 2]
    if len(pts) < MIN_PIXELS:
        return None
    mx = sum(x for x, _ in pts) / len(pts) + box[0] + 0.5
    my = sum(y for _, y in pts) / len(pts) + box[1] + 0.5
    return mx / w, my / h
