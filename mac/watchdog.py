"""Pure decision logic for the server's self-recovery (no I/O, no asyncio), so it can be unit-tested.

Stuck-button state: a mouse-button release the phone never received leaves iOS dragging. Seen as taps
landing far from where the tracked pointer should be, and as the board's hid.fail counter rising.
"""
import math
from collections.abc import Iterator


class StuckDetector:
    def __init__(self, miss_units: float = 150):
        self.miss_units = miss_units
        self._last_fail: int | None = None
        self._far_streak = 0

    def hid(self, fail: int) -> bool:
        """True once each time the board's hid.fail counter rises."""
        rose = self._last_fail is not None and fail > self._last_fail
        self._last_fail = fail
        return rose

    def landing(self, aimed: tuple[float, float], landed: tuple[float, float]) -> bool:
        """True when two consecutive verified taps landed more than miss_units away from their aim."""
        far = math.dist(aimed, landed) > self.miss_units
        self._far_streak = self._far_streak + 1 if far else 0
        if self._far_streak >= 2:
            self._far_streak = 0
            return True
        return False


def backoff(first: float, cap: float, n: int) -> Iterator[float]:
    """first, 2*first, 4*first, ... capped at cap, n values."""
    d = first
    for _ in range(n):
        yield min(d, cap)
        d *= 2
