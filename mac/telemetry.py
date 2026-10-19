"""One JSON object per line, appended to a file, plus an in-memory ring of the latest events.

Dependency-free so it is importable by tests and by the server alike. Writing never raises: telemetry
must not take the agent down.
"""
import json
import time
from collections import Counter, deque
from pathlib import Path


class Telemetry:
    def __init__(self, path: Path, ring: int = 500, mkdir: bool = True):
        self.path = Path(path)
        self._ring: deque[dict] = deque(maxlen=ring)
        self.counters: Counter = Counter()
        if mkdir:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def event(self, kind: str, **fields) -> dict:
        row = {"t": time.time(), "kind": kind, **fields}
        self._ring.append(row)
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass
        return row

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def recent(self, n: int | None = None) -> list[dict]:
        rows = list(self._ring)
        return rows if n is None else rows[-n:]
