"""Everything received since the last keyframe, kept on disk so a restarted agent (or a reconnected link)
resumes the mirror where it stopped instead of replaying the board's whole buffer over BLE.

iOS sends one keyframe per mirroring session; every frame after it is a P-frame that needs all the ones
before. The board keeps them (4 MB) and replays them to a new `stream on` client, which takes minutes over
BLE and can overflow the buffer meanwhile. With the spool, the Mac re-feeds its own copy to a fresh decoder
(seconds, local) and asks the board for only what came after its last access unit (`stream on <ts>`).

Record: [flags u8][board ts u32][len u32][Annex-B data]. A file starts with the CONFIG (SPS/PPS) record
and the IDR; a CONFIG that precedes a P-frame is a board continuation and is appended, not restarted.
The sidecar JSON notes the board's uptime against the wall clock, to tell a rebooted board (whose
timestamps restarted) from the one the spool came from."""
from __future__ import annotations

import json
import struct
import time
from pathlib import Path

IDR, CONFIG = 4, 8  # video notify flags (docs/ble-protocol.md)
HDR = struct.Struct("<BII")
MAX_BYTES = 48 * 1024 * 1024  # debt: beyond this the spool stops and a restart replays; raise when decode time is measured
SIDECAR_EVERY_S = 5.0


class Spool:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.cfg: tuple[bytes, int] | None = None
        self.bytes = 0        # AU bytes since the keyframe (comparable with the board's status.gop)
        self.last_ts = 0      # board ts of the newest recorded AU
        self.n = 0
        self.full = False
        self._f = None
        self._board: tuple[int, float] | None = None  # (board up ms, wall clock) at the last note
        self._noted_at = 0.0

    # ---- writing ----------------------------------------------------------------------------------
    def record(self, au: bytes, flags: int, ts: int) -> None:
        if flags & CONFIG:
            self.cfg = (au, ts)
            return
        if flags & IDR:
            self.clear()
            if self.cfg is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._f = open(self.path, "wb")
            self._write(self.cfg[0], CONFIG, self.cfg[1])
        elif self._f is None or self.full:
            return
        if self.bytes + len(au) > MAX_BYTES:
            self.full = True
            return
        self._write(au, flags, ts)
        self.bytes += len(au)
        self.last_ts = ts
        self.n += 1
        if self._board and time.monotonic() - self._noted_at > SIDECAR_EVERY_S:
            self._write_sidecar()

    def _write(self, au: bytes, flags: int, ts: int) -> None:
        self._f.write(HDR.pack(flags, ts, len(au)))
        self._f.write(au)
        self._f.flush()

    def note_board(self, up_ms: int) -> None:
        """The board's uptime right now (from its status), so a later start can tell the same boot."""
        self._board = (int(up_ms), time.time())

    def _write_sidecar(self) -> None:
        up, wall = self._board
        self.path.with_suffix(".json").write_text(json.dumps({"up": up, "wall": wall, "last_ts": self.last_ts}))
        self._noted_at = time.monotonic()

    def clear(self) -> None:
        if self._f:
            self._f.close()
        self._f = None
        self.bytes = self.last_ts = self.n = 0
        self.full = False
        self.path.unlink(missing_ok=True)
        self.path.with_suffix(".json").unlink(missing_ok=True)

    # ---- reading ----------------------------------------------------------------------------------
    def load(self) -> list[tuple[bytes, int, int]]:
        """The recorded access units as (data, flags, ts), oldest first, or [] when there is nothing usable.
        Leaves the spool positioned to append (a P-frame goes after the last record)."""
        try:
            raw = self.path.read_bytes()
        except OSError:
            return []
        out, pos = [], 0
        while pos + HDR.size <= len(raw):
            flags, ts, n = HDR.unpack_from(raw, pos)
            if pos + HDR.size + n > len(raw):
                break  # a torn last record (the agent died mid-write): drop it
            out.append((raw[pos + HDR.size:pos + HDR.size + n], flags, ts))
            pos += HDR.size + n
        if len(out) < 2 or not out[0][1] & CONFIG or not out[1][1] & IDR:
            self.clear()
            return []
        self.cfg = (out[0][0], out[0][2])
        self.bytes = sum(len(a) for a, f, _ in out if not f & CONFIG)
        self.last_ts = out[-1][2]
        self.n = len(out) - 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "r+b")
        self._f.seek(pos)
        self._f.truncate()
        return out

    def same_boot(self, board_up_ms: int, tolerance_ms: int = 120_000) -> bool:
        """Is the board whose uptime is `board_up_ms` now the one the spool was recorded from? The sidecar's
        uptime, advanced by the wall-clock time since, must agree within `tolerance_ms`."""
        try:
            d = json.loads(self.path.with_suffix(".json").read_text())
            expected = d["up"] + (time.time() - d["wall"]) * 1000
        except (OSError, ValueError, KeyError):
            return False
        return abs(board_up_ms - expected) <= tolerance_ms
