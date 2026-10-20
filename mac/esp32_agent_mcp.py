# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2", "bleak>=0.22", "pillow>=10"]
# ///
"""ESP32 Agent as an MCP server for Claude on this Mac.

The board (hardware/hid_bridge) is plugged into the iPhone: it is the phone's USB mouse + keyboard and an
AirPlay receiver for the phone's screen. This server talks to the board over BLE exactly like the iOS app
does (docs/ble-protocol.md) and turns that into MCP tools: screenshot, tap, swipe, type, key, home, back.

Run:  mac/install_agent.sh  (launchd agent serving HTTP on 127.0.0.1:8765, registered in ~/.claude.json)
      uv run --script mac/esp32_agent_mcp.py  (stdio; only works from a parent app that declares Bluetooth use)

Coordinates everywhere are integers 0-1000 relative to the screenshot (x left->right, y top->bottom),
the same convention as prompts/actor.md. The pointer is relative (AssistiveTouch), so the server tracks it in
mouse units, re-homes it in the top-left corner now and then, and needs a one-time calibration
(calibrate_start -> calibrate_move -> calibrate_fit with where the pointer was seen).
"""
import asyncio
import json
import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

from bleak import BleakClient, BleakScanner
from mcp.server.mcpserver import MCPServer, Image

from absmap import ABS_MAX, AbsMap, fit as fit_abs
from apps import AppNotes
from bench import Trial, find_dots, match_dots, pick_winners, summarize, to_markdown
from calibration import Calibration
from pointer import Frame, changed_fraction, locate
from screen import keyboard_visible
from spool import Spool
from stroke import Curve, ink_pixels, plan_path, score as stroke_score
from telemetry import Telemetry
from watchdog import StuckDetector, backoff

# stdout is the MCP transport: log to stderr only
logging.basicConfig(stream=sys.stderr, level=os.environ.get("ESP32_AGENT_LOG", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("esp32-agent")

BASE = "6e400001-a9e8-4b1b-9c39-6d6f8a3c"
SVC, CMD, REPLY, STATUS, VIDEO, NET = (BASE + s for s in ("0000", "0001", "0002", "0003", "0004", "0005"))
START, END, IDR, CONFIG, BENCH, SNAP = 1, 2, 4, 8, 16, 32
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
HOME = Path(os.environ.get("ESP32_AGENT_HOME", "~/.esp32-agent")).expanduser()
CAL_FILE = HOME / "calibration.json"
ABS_FILE = HOME / "absmap.json"     # absolute-pointer map (the --abs firmware build)
CURVE_FILE = HOME / "curve.json"   # px per report vs step size (measure_curve)
STROKES = HOME / "strokes"         # frames from stroke_bench
TELEMETRY = Telemetry(HOME / "telemetry.jsonl")
NOTES = AppNotes(HOME / "apps.json")
SCAN_TIMEOUT = float(os.environ.get("ESP32_AGENT_SCAN", "15"))
KEEP_AWAKE_S = float(os.environ.get("ESP32_AGENT_KEEPAWAKE", "20"))  # idle seconds between pointer nudges; 0 = off

REHOME_EVERY = 10       # gestures between re-homing (cancels drift of the relative pointer)
# Re-homing: a paced move far past the top-left corner (iOS clamps the pointer there). The board's `home`
# command fires 50 unpaced (-127,-127) reports 2 ms apart, and iOS 26 was seen dropping most of them (with
# the on-screen keyboard up), which left every following tap starting from the wrong place.
# The pointer cannot enter the display's rounded corner (radius ~110 px of 718): pushed into the corner it
# rests on that arc where the push direction is normal to it. A steep diagonal (-600 -1200) parked it at the
# top of the arc, 15% in from the left edge, so nothing left of that was reachable with positive units; the
# x-dominant push parks it on the left edge just below the arc (measured 4 of 4 at (3, 108) px). ~1 s paced.
HOME_CMD = "move -4800 -2400"  # sent at pace 40/4 (0.5 s): long enough to clamp at any tracking speed (measured
# 2026-10-19: at the minimum tracking speed the screen is ~1100 x 2400 units at step 10, ~4x more than at the
# default speed; the gain grows with the step size, 0.64/0.88/1.7 px per unit at steps 10/20/80, and does not
# depend on the report interval)
HOME_PACE = "40 4"
# bench 2026-10-19 (three runs, 5 taps per setting): 10/4 is ~30% faster per step than the firmware default
# 10/8 (1.85 s vs 2.6 s) with the same error band (15-22 vs 13-19 units) and no drops; 20/8 is as fast but
# misses more (29-37 units); 5/8 is slower and no more accurate. Sent at connect and after every flick.
DEFAULT_PACE = "10 4"
HOLD = 0.25             # rest before lifting a swipe, so iOS adds no momentum
TAP_GAP = 0.15          # stay still after a click, so iOS sees a tap and not the start of a drag


# ----------------------------------------------------------------------------------------------- BLE link

class Reassembler:
    """Rebuilds H.264 access units from `video` notifies and hands each to `on_au`. Starts, and after a seq
    gap resumes, only at the next CONFIG/IDR (P-frames need every frame before them)."""

    def __init__(self, on_au):
        self.on_au = on_au
        self.expect = None
        self.cur = None
        self.waiting = True
        self.gaps = self.bytes = self.aus = 0
        self.gop_bytes = 0  # AU bytes since the last CONFIG: comparable with the board's status.gop
        self.last_ts = 0    # board ts of the newest complete AU
        self.last_at = None

    def resume(self, gop_bytes: int, last_ts: int):
        """We already hold the GOP up to board time last_ts (the spool): accept P-frames straight away."""
        self.waiting = False
        self.gop_bytes, self.last_ts = gop_bytes, last_ts

    def feed(self, data: bytes):
        self.last_at = time.monotonic()
        self.bytes += len(data)
        seq, flags = struct.unpack_from("<HB", data)
        payload = data[3:]
        if self.expect is not None and seq != self.expect:
            self.gaps += 1
            self.cur, self.waiting = None, True
        self.expect = (seq + 1) & 0xFFFF
        if flags & BENCH:
            return
        if flags & START:
            if self.waiting and not flags & (CONFIG | IDR):
                return
            self.waiting = False
            ts = struct.unpack_from("<I", payload)[0]  # board millis() when the AU arrived from the phone
            self.cur = (flags, ts, bytearray(payload[4:]))
        elif self.cur is not None:
            self.cur[2].extend(payload)
        if flags & END and self.cur is not None:
            flags, ts, au = self.cur[0] | flags, self.cur[1], bytes(self.cur[2])
            self.aus += 1
            if flags & CONFIG:
                self.gop_bytes = 0
            else:
                self.gop_bytes += len(au)
                self.last_ts = ts
            self.on_au(au, flags, ts)
            self.cur = None


class Decoder:
    """One long-lived ffmpeg: Annex-B H.264 in on stdin, JPEG frames out on stdout; keeps the newest frame.
    iOS sends one keyframe per mirroring session and tiny P-frames forever after (even for a still screen),
    so decoding continuously is the only way to have a fresh frame without re-fetching the whole GOP."""

    def __init__(self):
        self.proc = None
        self.png: bytes | None = None
        self.at = 0.0
        self.frames = 0
        self.fed = 0
        self.err = ""
        self.pending: deque[int] = deque()  # board ts of fed AUs not yet decoded (one frame per AU)
        self.frame_ts = 0  # board ts of the newest decoded frame

    async def start(self):
        await self.stop()
        self.png, self.at, self.frames, self.fed, self.err = None, 0.0, 0, 0, ""
        self.pending.clear()
        self.frame_ts = 0
        self.proc = await asyncio.create_subprocess_exec(
            # measured: `-fflags nobuffer` makes ffmpeg emit nothing until EOF on a pipe, and frame threading
            # (the default) holds ~16 frames back; this combination decodes about one frame behind the input
            # and `-flags low_delay` stops the h264 decoder parking up to 16 frames in its reorder buffer
            FFMPEG, "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0", "-threads", "1",
            "-flags", "low_delay", "-f", "h264", "-i", "pipe:0", "-fps_mode", "passthrough", "-threads", "1",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-",
            # output -threads 1: the encoder must not pipeline either; JPEG because single-threaded PNG encoding of
            # a 718x1560 frame runs at ~3 fps and fell behind the mirror
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        asyncio.create_task(self._read_frames(self.proc))
        asyncio.create_task(self._read_errors(self.proc))

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                self.proc.kill()
            except ProcessLookupError:
                pass
        self.proc = None

    def feed(self, au: bytes, flags: int, ts: int):
        if self.proc and self.proc.returncode is None:
            self.fed += 1
            if flags & CONFIG:
                self.pending.clear()  # SPS/PPS decode to nothing; a (re)start also resyncs the ts queue
            else:
                self.pending.append(ts)
            self.proc.stdin.write(au)

    async def _read_frames(self, proc):
        buf = bytearray()
        while True:
            chunk = await proc.stdout.read(1 << 16)
            if not chunk:
                break
            buf += chunk
            while (i := buf.find(b"\xff\xd9")) >= 0:  # JPEG EOI; cannot occur inside entropy-coded data
                self.png, self.at, self.frames = bytes(buf[:i + 2]), time.monotonic(), self.frames + 1
                if self.pending:
                    self.frame_ts = self.pending.popleft()
                del buf[:i + 2]
        log.warning("ffmpeg exited (%s)", proc.returncode)

    async def _read_errors(self, proc):
        while line := await proc.stderr.readline():
            self.err = line.decode(errors="replace").strip()
            log.debug("ffmpeg: %s", self.err)


class Link:
    """One BLE connection to the board, (re)made on demand. Commands are `<id> <line>` -> `<id> ok|err ...`."""

    def __init__(self):
        self.client: BleakClient | None = None
        self.next_id = 1
        self.pending: dict[str, asyncio.Future] = {}
        self.status: dict = {}
        self.status_at = 0.0
        self.decoder = Decoder()
        self.spool = Spool(HOME / "gop.bin")  # our copy of the GOP, so a restart resumes instead of replaying
        self.video = Reassembler(self._on_au)
        self.write_lock = asyncio.Lock()
        self.connect_lock = asyncio.Lock()
        self.links = 0  # bumped per (re)connect, so the pointer knows to release and re-home
        self.name = None
        self.last_cmd_at = 0.0
        self.nudges = 0
        self._keeper = None
        self._reconnector = None
        self._recovering = None
        self.stuck = StuckDetector()

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    async def ensure(self):
        async with self.connect_lock:
            if self.connected:
                return
            log.info("scanning for 'ESP32 Agent ...'")
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or ad.local_name or "").startswith("ESP32 Agent"), timeout=SCAN_TIMEOUT)
            if not dev:
                raise RuntimeError("board not found over BLE: is it plugged into the iPhone (powered) and within a "
                                   "few meters of this Mac?")
            client = BleakClient(dev, timeout=20, disconnected_callback=self._on_disconnect)
            await client.connect()
            await client.start_notify(REPLY, self._on_reply)
            self.status_at = 0.0
            await client.start_notify(STATUS, self._on_status)
            self.video = Reassembler(self._on_au)
            await self.decoder.start()
            resume_ts = await self._refeed_spool()
            await client.start_notify(VIDEO, lambda _, d: self.video.feed(bytes(d)))
            self.client, self.name, self.links = client, dev.name, self.links + 1
            log.info("connected to %s (%s), mtu %s", dev.name, dev.address, client.mtu_size)
            TELEMETRY.event("link", state="connected", name=dev.name, mtu=client.mtu_size)
            await self._stream_on(resume_ts)  # keep the mirror flowing into the decoder for as long as we are linked
            await self._cmd(f"pace {DEFAULT_PACE}")  # the board boots at 10/8; see DEFAULT_PACE
            if KEEP_AWAKE_S and (self._keeper is None or self._keeper.done()):
                self._keeper = asyncio.create_task(self._keep_awake())

    def _on_au(self, au: bytes, flags: int, ts: int):
        self.spool.record(au, flags, ts)
        self.decoder.feed(au, flags, ts)

    async def _refeed_spool(self) -> int:
        """Decode our own copy of the GOP (seconds, local) so the board only has to send what came after it.
        Returns the board timestamp to resume from, or 0 when there is nothing usable (another boot, no spool)."""
        for _ in range(20):  # the first status (1 Hz) says which boot the board is on
            if self.status_at:
                break
            await asyncio.sleep(0.1)
        aus = self.spool.load()
        if not aus:
            return 0
        up = int(self.status.get("up", 0))
        if not self.status_at or not self.spool.same_boot(up):
            TELEMETRY.event("replay", resume=False, why="board rebooted or no sidecar", spool_aus=len(aus))
            self.spool.clear()
            return 0
        for au, flags, ts in aus:
            self.decoder.feed(au, flags, ts)
        self.video.resume(self.spool.bytes, self.spool.last_ts)
        TELEMETRY.event("replay", resume=True, spool_aus=len(aus), spool_bytes=self.spool.bytes, last_ts=self.spool.last_ts)
        log.info("re-fed %d access units (%d KB) from the spool; resuming at board ts %d",
                 len(aus), self.spool.bytes // 1024, self.spool.last_ts)
        return self.spool.last_ts

    async def _stream_on(self, resume_ts: int):
        if resume_ts:
            try:
                await self._cmd(f"stream on {resume_ts}")
                return
            except RuntimeError as e:  # firmware without resume support: replay from the keyframe
                if "usage" not in str(e):
                    raise
                log.warning("board does not support 'stream on TS' (old firmware): replaying")
                self.video = Reassembler(self._on_au)  # the notify handler reads self.video at call time
        await self._cmd("stream on")

    async def _keep_awake(self):
        """While nothing else is happening, wiggle the pointer by one unit every KEEP_AWAKE_S seconds: iOS counts
        mouse input as user activity, so an unlocked phone never idles to the lock screen (which would stop
        mirroring and suspend USB). The board cannot unlock a locked phone (Face ID); errors are ignored."""
        while True:
            await asyncio.sleep(2)
            if not self.connected or time.monotonic() - self.last_cmd_at < KEEP_AWAKE_S:
                continue
            if self.status.get("usb") != "ok":
                continue
            try:
                await self._cmd("move 1 0")
                await self._cmd("move -1 0")
                self.nudges += 1
            except Exception as e:
                log.debug("keep-awake nudge failed: %s", e)

    def _on_disconnect(self, _):
        log.warning("board disconnected")
        TELEMETRY.event("link", state="disconnected")
        if self._reconnector is None or self._reconnector.done():
            self._reconnector = asyncio.get_running_loop().create_task(self._reconnect())
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("board disconnected"))
        self.pending.clear()

    async def _reconnect(self):
        """After a drop, try to come back on our own so the stream and keep-awake resume without a tool call."""
        for delay in backoff(1, 15, 8):
            await asyncio.sleep(delay)
            if self.connected:
                return
            try:
                await self.ensure()
                TELEMETRY.event("recover", action="reconnect", result="ok", after_s=delay)
                return
            except Exception as e:
                TELEMETRY.event("recover", action="reconnect", result=str(e)[:120], after_s=delay)
        log.warning("gave up reconnecting; the next tool call will try again")

    async def usb_reset(self, why: str) -> str:
        """Ask the board to re-enumerate (like a re-plug), then re-home the pointer."""
        TELEMETRY.event("recover", action="usbreset", why=why)
        try:
            await self._cmd("usbreset")
            await asyncio.sleep(3)  # the phone mounts the device again
            phone._pos = None
            return "usb reset sent; pointer will re-home on the next gesture"
        finally:
            self._recovering = None

    def _on_reply(self, _, data: bytearray):
        text = data.decode(errors="replace")
        rid, _, rest = text.partition(" ")
        if rid in ("log", "app"):
            log.info("[%s] %s", rid, rest)
            TELEMETRY.event("boardlog", text=rest)
            return
        fut = self.pending.pop(rid, None)
        if fut and not fut.done():
            fut.set_result(rest)

    def _on_status(self, _, data: bytearray):
        try:
            self.status = json.loads(data)
            self.status_at = time.monotonic()
            self.spool.note_board(int(self.status.get("up", 0)))
            fail = int(self.status.get("hid", {}).get("fail", 0))
            if self.stuck.hid(fail) and self._recovering is None:
                self._recovering = asyncio.get_running_loop().create_task(self.usb_reset("hid.fail rose"))
        except ValueError:
            pass

    def board_now(self) -> int:
        """Estimated board millis() right now (status carries `up` once a second)."""
        return int(self.status.get("up", 0) + (time.monotonic() - self.status_at) * 1000) if self.status_at else 0

    @staticmethod
    def _timeout(line: str) -> float:
        name, _, arg = line.partition(" ")
        if name == "move":
            return 5 + max(abs(int(n)) for n in arg.split()) / 10 * 0.012
        if name == "type":
            return 5 + 0.1 * len(arg)
        if name in ("snap", "bench"):
            return 60
        return 8

    async def cmd(self, line: str, timeout: float | None = None) -> str:
        await self.ensure()
        return await self._cmd(line, timeout)

    async def _cmd(self, line: str, timeout: float | None = None) -> str:
        rid = str(900000 + self.next_id)  # the iOS app, if also connected, uses ids 100000-800000
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        t0 = time.monotonic()
        try:
            async with self.write_lock:
                await self.client.write_gatt_char(CMD, f"{rid} {line}".encode(), response=True)
            reply = await asyncio.wait_for(fut, timeout or self._timeout(line))
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            TELEMETRY.event("cmd", line=line, reply=None, ms=None, timeout=True)
            raise RuntimeError(f"no reply from the board to '{line}' (link busy or dropped)")
        self.last_cmd_at = time.monotonic()
        TELEMETRY.event("cmd", line=line, reply=reply, ms=round((time.monotonic() - t0) * 1000, 1))
        log.debug("%s -> %s (%.0f ms)", line, reply, (time.monotonic() - t0) * 1000)
        if reply.startswith("err"):
            raise RuntimeError(f"board: {reply}")
        return reply

    async def net(self) -> dict:
        await self.ensure()
        return json.loads(await self.client.read_gatt_char(NET))  # encrypted: first read triggers the macOS pairing prompt

    LIVE_BYTES = 8 * 1024  # not-yet-sent bytes still counted as live (idle P-frames are ~300 B, so 64 KB would be 40 s)
    LIVE_PENDING = 2        # access units fed to ffmpeg but not decoded yet

    def lag(self) -> dict:
        """How far the decoded picture is from the phone's present: bytes the board still has to send
        (its status.gop minus what arrived since the keyframe), access units not yet decoded, and the age
        of the newest frame (the phone sends frames only while something on screen changes)."""
        v, d = self.video, self.decoder
        todo = max(0, int(self.status.get("gop", 0)) - v.gop_bytes) if self.status_at else None
        pending = len(d.pending)
        age = round((self.board_now() - v.last_ts) / 1000, 1) if v.last_ts and self.status_at else None
        mirroring = self.status.get("ap") == "streaming"
        # gop == 0 while mirroring: the board's 4 MB buffer overflowed (or no keyframe yet) and it forwards
        # nothing until iOS sends the next keyframe, which iOS only does when a mirroring session starts
        blind = mirroring and self.status_at and int(self.status.get("gop", 0)) == 0
        live = (mirroring and not blind and d.png is not None and todo is not None and todo <= self.LIVE_BYTES
                and pending <= self.LIVE_PENDING)
        return {"live": live, "mirroring": mirroring, "waiting_for_keyframe": bool(blind),
                "replay_bytes_to_go": todo, "pending_decodes": pending, "newest_frame_age_s": age,
                "ble_kbps": self.status.get("ble", {}).get("kbps")}

    async def frame(self, timeout: float = 8.0) -> tuple[bytes, dict]:
        """The newest decoded frame and its lag. Waits up to `timeout` s for a live one; after a (re)connect
        the board first replays its whole buffer since the keyframe, so the frame can be well behind:
        callers must say so instead of acting on it."""
        await self.ensure()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lag = self.lag()
            if lag["live"]:
                return self.decoder.png, lag
            await asyncio.sleep(0.1)
        lag = self.lag()
        if self.decoder.png:
            return self.decoder.png, lag
        v = self.video
        if not v.bytes:
            raise RuntimeError("no video from the board yet: is the iPhone mirroring to 'ESP32 Agent'? "
                               f"(board: ap={self.status.get('ap')}, gop={self.status.get('gop')} bytes)")
        raise RuntimeError(f"video arriving ({v.bytes} bytes, {v.aus} access units) but no decodable frame yet: "
                           "the board is replaying the mirror session's keyframe and everything since it "
                           f"({self.status.get('gop')} bytes over BLE), or iOS has not sent a keyframe. "
                           "Fastest fix: stop and restart Screen Mirroring on the phone.")


# ---- pointer finding (Link methods, bound below) -------------------------------------------------------

NUDGE = 30          # mouse units the pointer is wiggled by to find it (12 barely moved it: tiny moves travel less)
SETTLE_S = 1.5      # max wait for a still screen
FRESH_S = 2.5       # max wait for a frame after the nudge


async def _settled_frame(self: Link) -> Frame:
    """A frame taken when two consecutive frames barely differ (the screen is still)."""
    deadline = time.monotonic() + SETTLE_S
    png, lag = await self.frame()
    live_deadline = time.monotonic() + 6  # a screen change bursts frames; the link catches up in a few seconds
    while not lag["live"] and lag["mirroring"] and time.monotonic() < live_deadline:
        await asyncio.sleep(0.5)
        png, lag = await self.frame(timeout=2)
    if not lag["live"]:
        raise RuntimeError("the mirror is not live (replaying or stopped); try again in a moment")
    prev = Frame.from_jpeg(png)
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        cur = Frame.from_jpeg((await self.frame())[0])
        if changed_fraction(prev, cur) < 0.004:  # a ticking clock icon alone is ~0.3%
            return cur
        prev = cur
    return prev


async def _fresh_frame(self: Link, after_board_ms: int, wait_s: float = FRESH_S) -> Frame | None:
    """The first frame the phone captured after board time `after_board_ms` (plus the phone's own encode
    latency), or None within wait_s. Decode time is useless here: the mirror runs ~0.5 s behind."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        if self.decoder.frame_ts >= after_board_ms + 150:
            return Frame.from_jpeg(self.decoder.png)
    return None


async def _find_pointer(self: Link, near_right_edge: bool = False, nudge: int = NUDGE) -> tuple[float, float] | None:
    """Where the pointer is now, as screen fractions; None if it could not be told apart from the screen.
    iOS hides an idle pointer, so: nudge out (it shows and moves), capture, nudge back, capture; the two
    footprints between those frames give the position, the one opposite the second nudge being the current."""
    sign = -1 if near_right_edge else 1
    await self._settled_frame()  # live and still
    await self._cmd(f"move {sign * nudge} 0")
    out = await self._fresh_frame(self.board_now())
    await self._cmd(f"move {-sign * nudge} 0")
    back = await self._fresh_frame(self.board_now())
    if out is None or back is None:
        TELEMETRY.event("pointer", found=False, why="no fresh frame after a nudge")
        return None
    found = locate(out, back, -sign)  # out -> back moved by -sign
    TELEMETRY.event("pointer", found=found is not None, at=found[1] if found else None,
                    changed=round(changed_fraction(out, back), 4))
    if found is None:  # keep the pair for inspection (last 20 misses)
        d = HOME / "debug"
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        (d / f"pointer-{stamp}-out.jpg").write_bytes(out.jpeg)
        (d / f"pointer-{stamp}-back.jpg").write_bytes(back.jpeg)
        for old_file in sorted(d.glob("pointer-*.jpg"))[:-40]:
            old_file.unlink()
    return found[1] if found else None


async def _frame_changed(self: Link, before: Frame, after_board_ms: int, wait_s: float = 8.0,
                         min_change: float = 0.25) -> bool | None:
    """Did at least min_change of the screen change relative to `before`, in a frame the phone captured after
    board time `after_board_ms`? A screen change bursts frames the link needs seconds to drain, so frames are
    chosen by board timestamp, not by link liveness. None when no such frame arrived within wait_s."""
    deadline = time.monotonic() + wait_s
    seen = False
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        if self.decoder.png is None or self.decoder.frame_ts < after_board_ms + 300:
            continue
        seen = True
        if changed_fraction(before, Frame.from_jpeg(self.decoder.png)) >= min_change:
            return True
    return False if seen else None


async def _keyboard_is(self: Link, up: bool, wait_s: float = 8.0, after_board_ms: int | None = None) -> bool:
    """Is the on-screen keyboard showing (`up`) or gone (not `up`) in a frame the phone captured after
    `after_board_ms` (default: now)? Frames are chosen by board timestamp because the link may still be
    draining a burst, which can take longer than the wait: False then means "not seen", not "not there"."""
    if after_board_ms is None:
        after_board_ms = self.board_now() - 1500  # the keyboard may already be up: accept recent frames
    deadline = time.monotonic() + wait_s
    while True:
        if self.decoder.png is not None and self.decoder.frame_ts >= after_board_ms + 300:
            if keyboard_visible(Frame.from_jpeg(self.decoder.png)) == up:
                return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.3)


async def _keyboard_up(self: Link, wait_s: float = 8.0, after_board_ms: int | None = None) -> bool:
    return await self.keyboard_is(True, wait_s, after_board_ms)


Link._settled_frame = _settled_frame
Link._fresh_frame = _fresh_frame
Link.find_pointer = _find_pointer
Link.frame_changed = _frame_changed
Link.keyboard_is = _keyboard_is
Link.keyboard_up = _keyboard_up


def shot(png: bytes, lag: dict):
    """Tool result: the frame, plus a warning when it is a replay rather than the present."""
    img = Image(data=png, format="jpeg")
    if lag["live"]:
        return img
    if not lag["mirroring"]:
        return [img, f"WARNING: the phone is NOT mirroring any more (it stops when the phone locks); this frame is "
                     f"{lag['newest_frame_age_s']} s old. Unlock the phone and choose Control Center > Screen "
                     "Mirroring > ESP32 Agent again, then take a new screenshot."]
    if lag["waiting_for_keyframe"]:
        return [img, f"WARNING: the board's video buffer overflowed (4 MB since the keyframe) and it forwards nothing "
                     f"until iOS sends a new keyframe; this frame is {lag['newest_frame_age_s']} s old. Stop and "
                     "restart Screen Mirroring to ESP32 Agent on the phone, then take a new screenshot."]
    kb, kbps = (lag["replay_bytes_to_go"] or 0) / 1024, lag.get("ble_kbps") or 0
    eta = f", about {kb * 8 / kbps:.0f} s to go at {kbps} kbps" if kbps else ""
    return [img, f"WARNING: not live. The board is replaying its video buffer after a (re)connect: {kb:.0f} KB "
                 f"still to arrive{eta}, {lag['pending_decodes']} frames undecoded. Wait and take another "
                 "screenshot before acting on this one (status shows video.live)."]


# ----------------------------------------------------------------------------------------------- pointer

class Phone:
    """Touch-like gestures from relative mouse moves (ported from the legacy server/hidagent/phone.py)."""

    def __init__(self, link: Link):
        self.absmap = AbsMap.load(ABS_FILE)
        self.link = link
        self.cal = Calibration.from_dict(json.loads(CAL_FILE.read_text()) if CAL_FILE.exists() else None)
        self._pos = None
        self._moves = 0
        self._link = 0

    @property
    def abs_mode(self) -> bool:
        """The board enumerates as an absolute-coordinate mouse (flash.sh --abs): the pointer goes where told."""
        return self.link.status.get("ptr") == "abs"

    def _abs(self, x: float, y: float) -> tuple[int, int]:
        return self.absmap.abs("x", x / 1000), self.absmap.abs("y", y / 1000)

    async def abs_point(self, x: float, y: float):
        ax, ay = self._abs(x, y)
        await self.link.cmd(f"abs {ax} {ay}")
        self._pos = None

    async def abs_tap(self, x, y, precise: bool = False, dab: bool = False):
        await self.abs_point(x, y)
        ax, ay = self._abs(x, y)
        if precise:
            found = await self.link.find_pointer(nudge=self.nudge_units())
            if found is not None:
                fx, fy = found[0] * 1000, found[1] * 1000
                miss = ((fx - x) ** 2 + (fy - y) ** 2) ** 0.5
                TELEMETRY.event("aim", aimed=[x, y], landed=[round(fx), round(fy)], miss=round(miss, 1), mode="abs")
                if miss > 4:
                    ax += round((x - fx) / 1000 / self.absmap.ax)
                    ay += round((y - fy) / 1000 / self.absmap.ay)
                    ax, ay = max(0, min(ABS_MAX, ax)), max(0, min(ABS_MAX, ay))
                    await self.link.cmd(f"abs {ax} {ay}")
        if dab:
            d = max(1, round(0.015 / self.absmap.ax))  # ~1.5% of the width each way
            await self.link.cmd("press")
            try:
                await self.link.cmd(f"abs {max(0, ax - d)} {max(0, ay - d)}")
                await self.link.cmd(f"abs {min(ABS_MAX, ax + d)} {min(ABS_MAX, ay + d)}")
                await self.link.cmd(f"abs {ax} {ay}")
            finally:
                await self.link.cmd("release")
        else:
            await self.link.cmd("click")
        await asyncio.sleep(TAP_GAP)

    def _abs_line(self, x1, y1, x2, y2, step_units: float = 8.0) -> list[tuple[int, int]]:
        """Absolute points along a segment every `step_units` screen units (iOS wants motion, not a jump)."""
        n = max(1, int(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 / step_units))
        return [self._abs(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n) for i in range(1, n + 1)]

    async def abs_trace(self, pts: list[tuple[int, int]], dt_ms: int = 4):
        """Send absolute points as HID reports, batched per BLE command (about 40 per command)."""
        for i in range(0, len(pts), 40):
            chunk = pts[i:i + 40]
            await self.link.cmd(f"trace {dt_ms} " + " ".join(f"{x} {y}" for x, y in chunk))

    async def abs_drag(self, points, hold=True, step_units: float = 8.0, dt_ms: int = 4):
        await self.abs_point(*points[0])
        await self.link.cmd("press")
        try:
            pts = []
            for (x1, y1), (x2, y2) in zip(points, points[1:]):
                pts += self._abs_line(x1, y1, x2, y2, step_units)
            await self.abs_trace(pts, dt_ms)
            if hold:
                await asyncio.sleep(HOLD)
        finally:
            await self.link.cmd("release")
        self._pos = None

    async def abs_edge_gesture(self, start, end, hold_s: float = 0.0):
        """press at an edge, flick to `end` quickly, release (Home, Back, App Switcher)."""
        await self.link.cmd("release")
        await self.abs_point(*start)
        await asyncio.sleep(0.15)
        await self.link.cmd("press")
        try:
            await self.abs_trace(self._abs_line(start[0], start[1], end[0], end[1], step_units=25.0), dt_ms=2)
            if hold_s:
                await asyncio.sleep(hold_s)
        finally:
            await self.link.cmd("release")
        self._pos = None

    async def calibrate_abs(self) -> dict:
        """Command a 3x3 grid of absolute positions, read each back with the pointer finder, fit the map."""
        xs, ys = [], []
        grid = (0.15, 0.5, 0.85)
        missed = 0
        guess = self.absmap
        for fy in grid:
            for fx in grid:
                ax, ay = guess.abs("x", fx), guess.abs("y", fy)
                await self.link.cmd(f"abs {ax} {ay}")
                found = await self.link.find_pointer(near_right_edge=fx == grid[-1], nudge=self.nudge_units())
                if found is None:
                    missed += 1
                    continue
                xs.append((ax, found[0]))
                ys.append((ay, found[1]))
        if len(xs) < 4:
            raise RuntimeError(f"only {9 - missed} of 9 grid points found; keep the screen still and retry")
        self.absmap = fit_abs(xs, ys)
        self.absmap.save(ABS_FILE)
        TELEMETRY.event("calibrate", mode="abs", missed=missed, residual=self.absmap.residual, map=self.absmap.to_dict())
        return {"mode": "abs", "grid_points_found": 9 - missed, "residual_frac": self.absmap.residual,
                "map": self.absmap.to_dict()}

    @property
    def calibrated(self) -> bool:
        if self.abs_mode:
            return True  # the default map spans the device range; calibrate_abs refines it
        return self.cal.ready

    @property
    def span(self) -> dict | None:
        return self.cal.span()

    def save(self):
        HOME.mkdir(parents=True, exist_ok=True)
        CAL_FILE.write_text(json.dumps(self.cal.to_dict()))

    def fit(self, points):
        """Two-point affine fallback: [[units_x, units_y, screen_x, screen_y], ...] (screen 0-1000)."""
        if len(points) < 2:
            raise RuntimeError("need at least 2 points")
        cal = {}
        for ui, si, a, b in ((0, 2, "ax", "bx"), (1, 3, "ay", "by")):
            us = [float(pt[ui]) for pt in points]
            fs = [float(pt[si]) / 1000 for pt in points]
            mu, mf = sum(us) / len(us), sum(fs) / len(fs)
            var = sum((u - mu) ** 2 for u in us)
            if var < 1:
                raise RuntimeError("the points must differ on both axes")
            cal[a] = sum((u - mu) * (f - mf) for u, f in zip(us, fs)) / var
            cal[b] = mf - cal[a] * mu
            if cal[a] <= 0:
                raise RuntimeError("calibration points are inconsistent (negative scale)")
        self.cal = Calibration.from_dict(cal)
        self.save()
        self._pos = None
        return cal

    def _dx(self, x: float) -> int:
        return self.cal.units("x", min(max(x, 0), 1000) / 1000)

    def _dy(self, y: float) -> int:
        return self.cal.units("y", min(max(y, 0), 1000) / 1000)

    def span_units(self) -> tuple[int, int]:
        """Mouse units across the whole screen (width, height) by the current map; a default before calibration.
        Gestures, nudges and dabs are sized from this so they survive a change of tracking speed."""
        if not self.calibrated:
            return 270, 620
        return (max(60, round((self.cal.units("x", 0.9) - self.cal.units("x", 0.1)) / 0.8)),
                max(120, round((self.cal.units("y", 0.9) - self.cal.units("y", 0.1)) / 0.8)))

    def nudge_units(self) -> int:
        if self.abs_mode:
            return 30  # the abs build emulates `move` at ~1 px per unit
        return max(12, round(0.1 * self.span_units()[0]))  # ~10% of the width: clearly a different blob

    async def home(self):
        """Park the pointer on the left edge below the rounded corner, the origin of every tap."""
        if self.abs_mode:
            return
        await self.link.cmd(f"pace {HOME_PACE}")
        try:
            await self.link.cmd(HOME_CMD)
        finally:
            await self.link.cmd(f"pace {DEFAULT_PACE}")

    async def _move_to(self, tx: int, ty: int, rehome: bool = True):
        if self._link != self.link.links:  # new link: a dropped gesture may have left the button down
            await self.link.cmd("release")
            self._pos, self._link = None, self.link.links
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            await self.home()
            self._pos, self._moves = (0, 0), 0
        x, y = self._pos
        self._pos = None
        await self.link.cmd(f"move {tx - x} {ty - y}")
        self._pos, self._moves = (tx, ty), self._moves + 1

    async def point(self, x: float, y: float):
        """Put the pointer at x, y (0-1000) with one move from the homed corner. iOS ramps each separate move
        up, losing a near-constant 10-20 units at its start, so a chain of relative moves drifts; one move from
        a known corner is what the calibration measured and lands the same way every time."""
        if self.abs_mode:
            return await self.abs_point(x, y)
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated: run calibrate_auto")
        await self.link.cmd("release")
        await self.home()
        self._pos, self._moves = (0, 0), 0
        await self._move_to(self._dx(x), self._dy(y), rehome=False)

    async def reset(self):
        if self.abs_mode:
            self._pos = None
            await self.link.cmd("release")
            return
        self._pos = None
        await self.link.cmd("release")
        await self.home()
        self._pos, self._moves = (0, 0), 0

    async def edge_home(self):
        """The iOS Home gesture from the bottom edge, independent of calibration: pin the pointer to the bottom,
        press, flick up, release; then re-home the tracker."""
        if self.abs_mode:
            await self.abs_edge_gesture((500, 999), (500, 450))
            await asyncio.sleep(0.6)
            return
        w, h = self.span_units()
        await self.link.cmd("release")
        await self.link.cmd(f"pace {HOME_PACE}")
        await self.link.cmd(f"move 0 {3 * h}")  # paced; iOS clamps the pointer at the edge
        await self.link.cmd("pace 25 4")    # a fast flick: a slow drag that ends still reads as the App Switcher
        await self.link.cmd("press")
        try:
            await self.link.cmd(f"move 0 -{round(0.45 * h)}")
        finally:
            await self.link.cmd("release")
            await self.link.cmd(f"pace {DEFAULT_PACE}")
        await asyncio.sleep(0.6)
        await self.reset()

    async def edge_back(self):
        """iOS back: a flick from the pinned left edge rightwards."""
        if self.abs_mode:
            return await self.abs_edge_gesture((0, 500), (450, 500))
        w, h = self.span_units()
        await self.link.cmd("release")
        await self.link.cmd(f"pace {HOME_PACE}")
        await self.link.cmd(f"move -{3 * w} 0")
        await self.link.cmd("pace 25 4")
        await self.link.cmd("press")
        try:
            await self.link.cmd(f"move {round(0.45 * w)} 0")
        finally:
            await self.link.cmd("release")
            await self.link.cmd(f"pace {DEFAULT_PACE}")
        self._pos = None

    async def app_switcher(self):
        """Drag up from the bottom edge and hold before releasing: the App Switcher."""
        if self.abs_mode:
            return await self.abs_edge_gesture((500, 999), (500, 500), hold_s=0.5)
        w, h = self.span_units()
        await self.link.cmd("release")
        await self.link.cmd(f"pace {HOME_PACE}")
        await self.link.cmd(f"move 0 {3 * h}")
        await self.link.cmd(f"pace {DEFAULT_PACE}")
        await self.link.cmd("press")
        try:
            await self.link.cmd(f"move 0 -{round(0.45 * h)}")
            await asyncio.sleep(0.5)
        finally:
            await self.link.cmd("release")
        self._pos = None

    GRID = (0.15, 0.5, 0.85)  # screen fractions the grid aims at (from the current map, or a default span)

    async def calibrate_auto(self) -> dict:
        """Home, visit a 3x3 grid, find the pointer at each stop, fit the piecewise map for both axes. The grid
        is aimed with the previous map; when that map was for another pace or home it lands short of the far
        edge (measured: 0.75 instead of 0.85), so a second pass with the new map runs when coverage is short."""
        if self.abs_mode:
            return await self.calibrate_abs()
        default = Calibration.from_dict({"w": 269, "h": 618})
        try:
            result = await self._calibrate_pass(self.cal if self.cal.ready else default, self.nudge_units())
        except (RuntimeError, ValueError) as e:
            # a map from another tracking speed aims the probes (and the finder's nudge) up to 4x off:
            # the pointer is lost or clamps at the edges. Start over from the default span.
            log.warning("calibration with the stored map failed (%s); retrying from the default span", e)
            result = await self._calibrate_pass(default, NUDGE)
            result["fallback"] = True
        far = min(self.cal.knots["x"][-1][1], self.cal.knots["y"][-1][1])
        if far < 0.8:
            log.info("calibration reached only %.2f of the screen; second pass", far)
            result = await self._calibrate_pass(self.cal, self.nudge_units())
            result["passes"] = 2
        return result

    async def _calibrate_pass(self, guess: Calibration, nudge: int) -> dict:
        res = (int(self.link.status.get("w", 480)), int(self.link.status.get("h", 1040)))
        await self.reset()
        home = await self.link.find_pointer(nudge=nudge)
        if home is None:
            raise RuntimeError("pointer not found after homing: is AssistiveTouch on and the screen still?")
        # the guess only picks where to probe; the probes must be distinct positive moves (the old map can
        # say 0 units for a fraction left of its own origin, which would duplicate the home point)
        xs = self._spread([guess.units("x", f) for f in self.GRID])
        ys = self._spread([guess.units("y", f) for f in self.GRID])
        seen_x: dict[int, list[float]] = {u: [] for u in xs}
        seen_y: dict[int, list[float]] = {u: [] for u in ys}
        missed = 0
        for uy in ys:
            for ux in xs:
                await self.home()  # each stop is one move from the corner, exactly like a tap
                self._pos = (0, 0)
                await self._move_to(ux, uy, rehome=False)
                found = await self.link.find_pointer(near_right_edge=ux == xs[-1], nudge=nudge)
                if found is None:
                    missed += 1
                    continue
                seen_x[ux].append(found[0])
                seen_y[uy].append(found[1])
        px = [(0, home[0])] + [(u, sum(v) / len(v)) for u, v in seen_x.items() if v]
        py = [(0, home[1])] + [(u, sum(v) / len(v)) for u, v in seen_y.items() if v]
        if len(px) < 3 or len(py) < 3:
            raise RuntimeError(f"only {9 - missed} of 9 grid points found; keep the screen still and retry")
        self.cal = Calibration()
        self.cal.fit_grid("x", px, res)
        self.cal.fit_grid("y", py, res)
        self.save()
        await self.reset()
        TELEMETRY.event("calibrate", res=res, missed=missed, x=px, y=py)
        return {"res": res, "grid_points_found": 9 - missed, "x": px, "y": py, "span": self.span}

    @staticmethod
    def _spread(units: list[int], least: int = 15) -> list[int]:
        out: list[int] = []
        for u in units:
            out.append(max(u, (out[-1] + least) if out else least))
        return out

    async def tap(self, x, y, precise: bool = False, dab: bool = False):
        """dab: instead of a click, press and stroke the pointer 8 units out and back so a drawing tool leaves
        a mark centred on the aim (a plain click in Freeform's pen draws nothing visible, and a 2-unit wiggle
        is mostly swallowed by iOS pointer acceleration; measured 2026-10-19)."""
        if self.abs_mode:
            return await self.abs_tap(x, y, precise, dab)
        await self.point(x, y)
        if precise:
            found = await self.link.find_pointer(near_right_edge=x > 900, nudge=self.nudge_units())
            if found is not None:
                fx, fy = found[0] * 1000, found[1] * 1000
                miss = ((fx - x) ** 2 + (fy - y) ** 2) ** 0.5
                TELEMETRY.event("aim", aimed=[x, y], landed=[round(fx), round(fy)], miss=round(miss, 1))
                self.cal.learn("x", self._pos[0], found[0])
                self.cal.learn("y", self._pos[1], found[1])
                self.save()
                if self.link.stuck.landing((x, y), (fx, fy)) and self.link._recovering is None:
                    self.link._recovering = asyncio.get_running_loop().create_task(self.link.usb_reset("taps landing far off"))
                    raise RuntimeError("taps are landing far from their aim; resetting USB, try again in a few seconds")
                if miss > 8:
                    dx = self._dx(x) - self._dx(fx)
                    dy = self._dy(y) - self._dy(fy)
                    await self.link.cmd(f"move {dx} {dy}")
                    self._pos = (self._pos[0] + dx, self._pos[1] + dy)
        if dab:
            await self.link.cmd("press")
            try:
                # a ~3%-of-width stroke centred on the aim: the bench reads the mark's centroid, and a stroke
                # that starts at the aim puts it half a stroke away (about 10 units of bias)
                d = max(4, round(0.03 * self.span_units()[0]))
                await self.link.cmd(f"move {d // 2} {d // 2}")
                await self.link.cmd(f"move -{d} -{d}")
                await self.link.cmd(f"move {d // 2} {d // 2}")
            finally:
                await self.link.cmd("release")
        else:
            await self.link.cmd("click")
        await asyncio.sleep(TAP_GAP)

    async def long_press(self, x, y, seconds: float):
        if self.abs_mode:
            await self.abs_point(x, y)
            await self.link.cmd("press")
            try:
                await asyncio.sleep(seconds)
            finally:
                await self.link.cmd("release")
            return
        await self.point(x, y)
        await self.link.cmd("press")
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.link.cmd("release")

    async def drag(self, points, hold=True):
        if self.abs_mode:
            return await self.abs_drag(points, hold)
        await self.point(*points[0])
        await self.link.cmd("press")
        try:
            for x, y in points[1:]:
                await self._move_to(self._dx(x), self._dy(y), rehome=False)
            if hold:
                await asyncio.sleep(HOLD)
        finally:
            await self.link.cmd("release")

    async def swipe(self, x1, y1, x2, y2, hold=True):
        await self.drag([(x1, y1), (x2, y2)], hold)


# ----------------------------------------------------------------------------------------------- MCP tools

mcp = MCPServer("esp32-agent", instructions=(
    "Tools to see and operate a real iPhone through an ESP32-S3 board (AirPlay mirror in, USB mouse/keyboard out). "
    "Coordinates are 0-1000 relative to the screenshot. Always take a screenshot before acting and after an "
    "action to check its effect. First use on a new board: calibrate_start, read the pointer position off the "
    "screenshot, calibrate_move, calibrate_fit. The phone must be unlocked, mirroring to 'ESP32 Agent', and have "
    "AssistiveTouch on."))
link = Link()
phone = Phone(link)


def _ok(msg: str) -> str:
    return msg


@mcp.tool()
async def status() -> str:
    """Board and link health: BLE connection, USB (phone) state, hotspot, AirPlay mirroring state and resolution,
    pointer calibration. Call this first when something does not work."""
    try:
        await link.ensure()
    except Exception as e:
        return json.dumps({"connected": False, "error": str(e), "calibrated": phone.calibrated})
    await link.cmd("ping")
    await asyncio.sleep(1.2)  # a status notify comes every second
    st = dict(link.status)
    hints = []
    if st.get("usb") != "ok":
        hints.append("usb is not 'ok': plug the board into the iPhone and unlock the phone")
    if st.get("ap") != "streaming":
        hints.append("ap is not 'streaming': on the iPhone join the board's hotspot (hotspot_info) and choose "
                     "Control Center > Screen Mirroring > ESP32 Agent")
    if not phone.calibrated:
        hints.append("pointer not calibrated: calibrate_start, calibrate_move, calibrate_fit")
    d = link.decoder
    lag = link.lag()
    video = {"ble_bytes": link.video.bytes, "access_units": link.video.aus, "gaps": link.video.gaps,
             "frames_decoded": d.frames, **lag, "ffmpeg": d.err}
    if lag["waiting_for_keyframe"]:
        hints.append("blind: the board's buffer overflowed and waits for a keyframe; stop and restart Screen "
                     "Mirroring to ESP32 Agent on the phone")
    elif d.png and lag["mirroring"] and not lag["live"]:
        hints.append("not live: the board is replaying its buffer after a (re)connect (video.replay_bytes_to_go "
                     "at video.ble_kbps); wait, or restart Screen Mirroring on the phone to start from a fresh, "
                     "small keyframe")
    if st.get("ap") == "streaming" and not d.png:
        hints.append("mirroring but no frame decoded yet: waiting for the keyframe replay to finish, or for iOS "
                     "to send one (stop and restart Screen Mirroring on the phone to force a fresh keyframe)")
    return json.dumps({"connected": True, "board": link.name, "status": st, "video": video,
                       "keep_awake": {"every_s": KEEP_AWAKE_S, "nudges": link.nudges},
                       "calibrated": phone.calibrated, "span": phone.span, "calibration_model": phone.cal.model,
                       "pointer": "abs" if phone.abs_mode else "rel", "absmap_residual": phone.absmap.residual,
                       "hints": hints})


@mcp.tool()
async def diagnostics(n: int = 50) -> str:
    """The last n telemetry events (commands with round-trip times, frames, link changes, board log lines,
    recoveries) and the running counters. Use it to find out what happened when something misbehaved."""
    st = link.status
    return json.dumps({"counters": dict(TELEMETRY.counters), "board": {"hid": st.get("hid"), "ble": st.get("ble"),
                       "logdrop": st.get("logdrop")}, "events": TELEMETRY.recent(n)}, default=str)


@mcp.tool()
async def hotspot_info() -> str:
    """The board's Wi-Fi hotspot name and password (the iPhone joins it so it can mirror its screen to the board).
    The first call pairs the Mac with the board over Bluetooth (macOS shows a pairing prompt: accept it)."""
    return json.dumps(await link.net())


@mcp.tool()
async def screenshot():
    """The iPhone's screen right now (from the AirPlay mirror), as a PNG. Coordinates for tap/swipe are 0-1000 of
    this image's width/height. Needs the phone to be mirroring to 'ESP32 Agent'."""
    png, lag = await link.frame()
    TELEMETRY.event("frame", live=lag["live"], mirroring=lag["mirroring"], replay_bytes=lag["replay_bytes_to_go"],
                    age_s=lag["newest_frame_age_s"], pending=lag["pending_decodes"])
    if not lag["live"]:
        TELEMETRY.count("stale_screenshots")
    fr = Frame.from_jpeg(png)
    state = (f"state: {'live' if lag['live'] else 'replaying' if lag['mirroring'] else 'not mirroring'}, "
             f"keyboard {'up' if keyboard_visible(fr) else 'down'}, frame age {lag['newest_frame_age_s']} s")
    result = shot(png, lag)
    return result + [state] if isinstance(result, list) else [result, state]


CAL_FIRST = (60, 100)  # mouse units of the first calibration stop, from the homed top-left corner


@mcp.tool()
async def calibrate_start():
    """Pointer calibration, step 1: homes the pointer in the top-left corner and moves it 60 units right and 100
    down, then returns a screenshot. Find the AssistiveTouch pointer (a small grey circle) and note its center in
    0-1000 coordinates. Then call calibrate_move (e.g. dx=140, dy=350) for a second point, and calibrate_fit with
    both: [[60, 100, x1, y1], [200, 450, x2, y2]]. Two well-separated points are needed because iOS pointer
    motion has an offset as well as a scale."""
    await phone.reset()
    await link.cmd(f"move {CAL_FIRST[0]} {CAL_FIRST[1]}")
    await asyncio.sleep(0.7)
    return shot(*await link.frame())


@mcp.tool()
async def calibrate_move(dx: int, dy: int):
    """Pointer calibration, step 2: moves the pointer a further dx, dy mouse units (keep it on screen: on a
    phone with a fast pointer ~250 units cross the width) and returns a screenshot. Cumulative units from the
    corner = previous stop + (dx, dy)."""
    await link.cmd(f"move {dx} {dy}")
    await asyncio.sleep(0.7)
    return shot(*await link.frame())


@mcp.tool()
async def calibrate_fit(points: list[list[int]]) -> str:
    """Pointer calibration, step 3: fit and save. points = [[units_x, units_y, screen_x, screen_y], ...], at
    least two, screen coordinates 0-1000 of where the pointer's center was seen for those cumulative mouse units
    from the corner. Stored in ~/.esp32-agent/calibration.json."""
    cal = phone.fit(points)
    return (f"calibrated: {phone.span['w']} x {phone.span['h']} mouse units across the screen, offsets "
            f"{cal['bx'] * 1000:.0f}, {cal['by'] * 1000:.0f} (0-1000 units); saved")


@mcp.tool()
async def calibrate_auto() -> str:
    """Automatic pointer calibration: homes the pointer, visits a 3x3 grid and finds the pointer at each stop
    by nudging it and differencing frames, then fits a piecewise map per axis. Needs a still screen (the Home
    Screen is ideal) and takes about 30 s. Re-run after changing the mirror resolution or pace."""
    return json.dumps(await phone.calibrate_auto())


@mcp.tool()
async def find_pointer() -> str:
    """Where the AssistiveTouch pointer is right now (0-1000), found by nudging it 12 units and back and
    differencing two frames. Returns null if the screen was moving or the pointer could not be seen."""
    found = await link.find_pointer(nudge=phone.nudge_units())
    return json.dumps(None if found is None else [round(found[0] * 1000), round(found[1] * 1000)])


@mcp.tool()
async def set_calibration(span_w: int, span_h: int) -> str:
    """Set a scale-only calibration: mouse units needed to cross the whole screen width and height (no offset;
    calibrate_fit is more accurate)."""
    phone.cal = Calibration.from_dict({"ax": 1 / span_w, "bx": 0.0, "ay": 1 / span_h, "by": 0.0})
    phone.save()
    phone._pos = None
    return "saved"


@mcp.tool()
async def tap(x: int, y: int, precise: bool = False) -> str:
    """Tap the screen at x, y (0-1000, origin top-left). Aim at the visual center of the element. precise=true
    finds the pointer on screen first and corrects the miss before clicking (about 1 s slower): use it for
    small or tightly packed targets such as tab bars, list rows, toggles, icons under 60 units."""
    await phone.tap(x, y, precise)
    return _ok(f"tapped {x},{y}" + (" (precise)" if precise else ""))


@mcp.tool()
async def long_press(x: int, y: int, seconds: float = 1.0) -> str:
    """Press and hold at x, y for `seconds` (context menus, app icon jiggle, etc.)."""
    await phone.long_press(x, y, seconds)
    return _ok(f"held {x},{y} for {seconds}s")


@mcp.tool()
async def swipe(x1: int, y1: int, x2: int, y2: int, momentum: bool = False) -> str:
    """Drag a finger from x1,y1 to x2,y2 (0-1000). Without momentum the content stops exactly where the finger
    stops; prefer short swipes (100-250 units) and check the result. momentum=true flicks (system gestures)."""
    await phone.swipe(x1, y1, x2, y2, hold=not momentum)
    return _ok(f"swiped {x1},{y1} -> {x2},{y2}")


@mcp.tool()
async def path(points: list[list[int]]) -> str:
    """One continuous stroke through 2-20 points [[x,y], ...] (0-1000): drawing, multi-segment drags."""
    if not 2 <= len(points) <= 20:
        raise RuntimeError("2-20 points")
    await phone.drag([(int(x), int(y)) for x, y in points])
    return _ok(f"stroke through {len(points)} points")


@mcp.tool()
async def scroll(x: int, y: int, lines: int) -> str:
    """Mouse-wheel scroll at x, y. Positive lines scroll content up (like a wheel), negative down."""
    await phone.point(x, y)
    await link.cmd(f"scroll {lines} 0")
    return _ok(f"scrolled {lines} at {x},{y}")


@mcp.tool()
async def type_text(text: str, fix_caps: bool = True) -> str:
    """Type ASCII text with the USB keyboard into the focused field. Waits up to 2 s for the on-screen
    keyboard to be showing (tap the field first). iOS capitalises the first letter it receives from a
    hardware keyboard; with fix_caps the first character is retyped afterwards so the text is sent exactly."""
    if not text.isascii():
        raise RuntimeError("the board types ASCII only")
    if not await link.keyboard_up(6.0):  # recent frames: the field was tapped before this call
        TELEMETRY.event("nav", action="type_text", ok=False, why="no keyboard")
        return "no effect: no keyboard is showing; tap a text field first, then type"
    await link.cmd(f"type {text}")
    if fix_caps and text[:1].islower():
        # select the first character and retype it; harmless where iOS did not capitalise. Key presses need
        # a short gap or iOS drops the arrow (seen as a doubled first letter).
        for combo in ("cmd+left", "shift+right"):
            await asyncio.sleep(0.2)
            await link.cmd(f"key {combo}")
        await asyncio.sleep(0.2)
        await link.cmd(f"type {text[0]}")
        await asyncio.sleep(0.2)
        await link.cmd("key cmd+right")
    TELEMETRY.event("nav", action="type_text", chars=len(text), ok=True)
    return _ok(f"typed {len(text)} chars")


@mcp.tool()
async def key(combo: str) -> str:
    """Press a key or combo: enter, esc, tab, backspace, space, up/down/left/right, or combos like cmd+h, cmd+a."""
    await link.cmd(f"key {combo}")
    return _ok(f"pressed {combo}")


@mcp.tool()
async def home() -> str:
    """Go to the iOS Home Screen: the pointer is pushed hard against the bottom edge (so no calibration error can
    miss the home indicator) and flicked up. (Cmd+H works in some apps but not all; iOS 26 ignores the
    consumer-control Home key.)"""
    await phone.edge_home()
    return _ok("home")


@mcp.tool()
async def back() -> str:
    """iOS back gesture: a flick from the left screen edge. Reports whether the screen changed."""
    before = Frame.from_jpeg((await link.frame(timeout=2))[0])
    t_board = link.board_now()
    await phone.edge_back()
    ok = await link.frame_changed(before, t_board)
    TELEMETRY.event("nav", action="back", ok=ok)
    if ok is None:
        return "back sent, unverified (the mirror was replaying); take a screenshot"
    return _ok("back") if ok else "no effect: the screen did not change (not every screen has a back gesture)"


@mcp.tool()
async def app_switcher() -> str:
    """Open the App Switcher (drag up from the bottom edge and hold)."""
    before = Frame.from_jpeg((await link.frame(timeout=2))[0])
    t_board = link.board_now()
    await phone.app_switcher()
    ok = await link.frame_changed(before, t_board)
    TELEMETRY.event("nav", action="app_switcher", ok=ok)
    if ok is None:
        return "app switcher sent, unverified (the mirror was replaying); take a screenshot"
    return _ok("app switcher") if ok else "no effect: the screen did not change"


@mcp.tool()
async def open_app(name: str) -> str:
    """Open an app by name: Home, then Search (Cmd+Space), type the name, Enter. Verifies that the screen
    changed from the Home Screen. Use this instead of hunting for icons."""
    t_home = link.board_now()
    await phone.edge_home()
    # the reference must be a frame captured after the flick: the link can still be draining the previous
    # app's closing animation for 10+ s, and a key sent into that animation was seen to be swallowed
    home_frame = await link._fresh_frame(t_home + 1200, wait_s=15.0) or await link._settled_frame()
    t_board = link.board_now()
    await link.cmd("key cmd+space")
    # measured: the keyboard shows ~3.5 s after the key, but the burst from the previous app closing can hold
    # the link back for longer, so the wait is generous and the fallback only fires when the screen still
    # looks like the Home Screen (a tap on an already-open Search lands on a keyboard key)
    opened = await link.keyboard_up(8.0, t_board)
    via_pill = False
    if not opened and await link.frame_changed(home_frame, t_board, wait_s=3) is False:
        via_pill = True  # the shortcut was seen to stop being honoured mid-session
        t_board = link.board_now()
        await phone.tap(500, 822, precise=True)  # the Search pill
        opened = await link.keyboard_up(8.0, t_board)
        if opened:
            TELEMETRY.event("nav", action="open_app", name=name, via="search pill")
    elif not opened:
        opened = await link.keyboard_up(5.0, t_board)  # the screen did change: still draining
    if opened:
        NOTES.learn("home", "cmd+space", not via_pill)
    if not opened:
        TELEMETRY.event("nav", action="open_app", name=name, ok=False, why="search did not open")
        return "no effect: Search did not open from the Home Screen (is the phone unlocked on the Home Screen?)"
    await link.cmd(f"type {name}")
    await asyncio.sleep(1.2)  # Search ranks the top hit
    t_board = link.board_now()
    await link.cmd("key enter")
    await asyncio.sleep(1.0)
    ok = await link.frame_changed(home_frame, t_board, wait_s=8)
    dismissed = ok and await link.keyboard_is(False, 5.0, t_board)  # Search still up = nothing was opened
    TELEMETRY.event("nav", action="open_app", name=name, ok=bool(ok and dismissed), search_left_open=bool(ok and not dismissed))
    phone._pos = None
    if ok is None:
        return f"'{name}' sent to Search, unverified (the mirror was replaying); take a screenshot"
    if ok and not dismissed:
        return f"no effect: Search found nothing to open for '{name}' and is still showing (home() closes it)"
    return _ok(f"opened {name} (verify with a screenshot that it is the right app)") if ok else \
        f"no effect: nothing opened for '{name}' (no such app, or Search picked nothing)"


@mcp.tool()
async def search_in_app(text: str, app: str = "") -> str:
    """Open the current app's search and type `text`: Cmd+K (then Cmd+F) when the app honours it, per the
    notes in ~/.esp32-agent/apps.json; records what worked. Say which app is open in `app` when you know."""
    tried = []
    for combo in ("cmd+k", "cmd+f"):
        if app and NOTES.shortcut(app, combo) is False:
            continue
        t_board = link.board_now()
        await link.cmd(f"key {combo}")
        tried.append(combo)
        if await link.keyboard_up(5.0, t_board):
            if app:
                NOTES.learn(app, combo, True)
            await link.cmd(f"type {text}")
            TELEMETRY.event("nav", action="search_in_app", app=app, combo=combo, ok=True)
            return _ok(f"search opened with {combo}; typed {len(text)} chars")
        if app:
            NOTES.learn(app, combo, False)
    TELEMETRY.event("nav", action="search_in_app", app=app, tried=tried, ok=False)
    return f"no effect: {', '.join(tried) or 'no shortcut'} did not open a search field; tap the app's search control instead"


@mcp.tool()
async def reset_pointer() -> str:
    """Release the mouse button and re-home the pointer in the top-left corner (after a failed gesture)."""
    await phone.reset()
    return _ok("pointer released and homed")


@mcp.tool()
async def usb_reset() -> str:
    """Re-enumerate the board on the phone (same effect as unplugging it): clears a stuck mouse button, the
    state where moves no longer show and taps land where a drag ends. The pointer re-homes afterwards."""
    await link.ensure()
    return await link.usb_reset("requested")


@mcp.tool()
async def pace(step: int = 10, step_ms: int = 8) -> str:
    """Pointer move pacing on the board: units per report (1-127) and ms between reports (1-50). Not stored;
    the bench sweeps it. Changing it invalidates the calibration until you re-run it."""
    reply = await link.cmd(f"pace {step} {step_ms}")
    phone._pos = None
    return f"{reply}: pace {step} units / {step_ms} ms (recalibrate before precise work)"


BENCH_AXES = {
    "res":  ["480 1040", "360 780", "718 1560"],
    "fps":  ["5", "3", "8"],
    "coex": ["balance", "bt", "wifi"],
    "pace": [DEFAULT_PACE, "10 8", "5 8", "20 8"],  # first = baseline, restored (and recalibrated) at the end
    "aim":  ["plain", "precise"],
}
BENCH_PLAN = [(300, 250), (700, 250), (500, 500), (300, 750), (700, 750)]  # five taps per setting


async def _apply_setting(axis: str, value: str) -> bool:
    """Returns whether the pointer map was refitted for the setting (res and pace both change it)."""
    if axis in ("res", "fps", "coex", "pace"):
        await link.cmd(f"{axis} {value}")
    if axis in ("res", "pace"):
        await phone.calibrate_auto()
        return True
    return False


async def _clean_canvas() -> Frame:
    """A frame with no mark near a planned tap. The previous setting's undos take a moment to render; a
    'before' frame that still shows its marks hides the next setting's marks, which land within 20 units of
    them (measured: only the one target where the two settings differed most was ever counted)."""
    deadline = time.monotonic() + 8
    fr = None
    while time.monotonic() < deadline:
        fr = await link._fresh_frame(link.board_now(), wait_s=3) or fr
        if fr is not None and not any(abs(d[0] - px) + abs(d[1] - py) < 60
                                      for d in (find_dots(fr) or []) for px, py in BENCH_PLAN):
            return fr
    log.warning("bench: the canvas did not come clean; the next setting's marks may be hidden")
    return fr or Frame.from_jpeg((await link.frame())[0])


@mcp.tool()
async def bench(axes: list[str] = ["aim", "pace", "coex"], taps_per_setting: int = 5) -> str:
    """A/B bench: with a blank Freeform board open and its pen selected, taps a planned grid per setting,
    reads the dots back, and ranks settings by tap error then latency. Axes: aim, pace, coex (live) and res,
    fps (need a mirroring restart per setting). Writes ~/.esp32-agent/bench/<date>.md and trial telemetry.
    Restores the baseline at the end and presses Home."""
    png, lag = await link.frame()
    if not lag["live"] or find_dots(Frame.from_jpeg(png)) is None:
        return "refused: open a blank Freeform board (white canvas) with the pen tool selected, mirroring live"
    unknown = [a for a in axes if a not in BENCH_AXES]
    if unknown:
        return f"refused: unknown axes {unknown}; choose from {list(BENCH_AXES)}"
    trials: list[Trial] = []
    try:
        for axis in axes:
            for value in BENCH_AXES[axis]:
                calibrated = await _apply_setting(axis, value)
                before = await _clean_canvas()
                drops0 = int(link.status.get("ble", {}).get("drop", 0))
                lat = []
                for (x, y) in BENCH_PLAN[:taps_per_setting]:
                    t0 = time.monotonic()
                    await phone.tap(x, y, precise=(value == "precise") if axis == "aim" else False, dab=True)
                    await link._fresh_frame(link.board_now())
                    lat.append((time.monotonic() - t0) * 1000)
                after = Frame.from_jpeg((await link.frame())[0])
                found = find_dots(after) or []
                known = find_dots(before) or []
                new = [d for d in found if all(abs(d[0] - k[0]) + abs(d[1] - k[1]) > 20 for k in known)]
                errors = match_dots(BENCH_PLAN[:taps_per_setting], new)
                drops = int(link.status.get("ble", {}).get("drop", 0)) - drops0
                for e, l in zip(errors, lat):
                    tr = Trial(axis, value, e, round(l), drops, calibrated)
                    trials.append(tr)
                    TELEMETRY.event(**tr.row())
                for _ in range(taps_per_setting):
                    await link.cmd("key cmd+z")  # Freeform: undo one dot per tap
            await _apply_setting(axis, BENCH_AXES[axis][0])  # baseline back for this axis
    finally:
        await phone.edge_home()
    summary = summarize(trials)
    winners = pick_winners(summary)
    out = HOME / "bench"
    out.mkdir(parents=True, exist_ok=True)
    path = out / (time.strftime("%Y-%m-%d-%H%M") + ".md")
    path.write_text(f"# Bench {time.strftime('%Y-%m-%d %H:%M')}\n\n" + to_markdown(summary, winners))
    return json.dumps({"summary": summary, "winners": winners, "file": str(path)}, default=str)


# ---- precise strokes ------------------------------------------------------------------------------------

def _frame_px() -> tuple[int, int]:
    """Mirror frame size in px (the drawing coordinate space)."""
    fr = Frame.from_jpeg(link.decoder.png, scale=1)
    return fr.w, fr.h


def _load_curve() -> Curve:
    if not CURVE_FILE.exists():
        raise RuntimeError("no acceleration curve yet: run measure_curve (with a still screen, e.g. a blank Freeform board)")
    return Curve.from_dict(json.loads(CURVE_FILE.read_text()))


@mcp.tool()
async def measure_curve(steps: str = "2,3,4,6,8,10,13,16,20,25,32,40,50,64,80,100,127", travel_px: int = 350) -> str:
    """Measure iOS pointer acceleration: px one HID report travels as a function of its step size (mouse units).
    For each step, moves the pointer right by N reports of that step twice (N and 2N, sized to travel about
    `travel_px`) and reads the displacement with the pointer finder. Needs a still screen (a blank Freeform
    board). Saves ~/.esp32-agent/curve.json, which `draw` and `stroke_bench` use. Takes 1-2 minutes."""
    W, H = _frame_px()
    rows = []
    points = []
    guess = _load_curve() if CURVE_FILE.exists() else Curve([(1, 1.8), (10, 22.0), (127, 500.0)])
    for st in [int(x) for x in steps.split(",") if x.strip()]:
        n1 = max(1, min(40, round(travel_px / 2 / max(1.0, guess.px(st)))))
        per = []
        for n in (n1, 2 * n1):
            await phone.point(120, 300)  # a known start, clear of the corner, by the calibration
            p0 = await link.find_pointer(nudge=phone.nudge_units())
            if p0 is None:
                rows.append({"step": st, "n": n, "err": "pointer not found at the start"})
                continue
            await link.cmd(f"pace {st} 4")
            try:
                await link.cmd(f"move {st * n} 0")
            finally:
                await link.cmd(f"pace {DEFAULT_PACE}")
            p1 = await link.find_pointer(nudge=phone.nudge_units())
            phone._pos = None
            if p1 is None:
                rows.append({"step": st, "n": n, "err": "pointer not found after the move"})
                continue
            px = (p1[0] - p0[0]) * W
            rows.append({"step": st, "n": n, "px": round(px, 1), "px_per_report": round(px / n, 2)})
            per.append(px / n)
        if per:
            points.append((st, sum(per) / len(per)))
    if len(points) < 3:
        return json.dumps({"error": "too few steps measured", "rows": rows})
    curve = Curve(points)
    CURVE_FILE.write_text(json.dumps({**curve.to_dict(), "measured": time.strftime("%Y-%m-%d %H:%M"), "frame": [W, H]}))
    TELEMETRY.event("curve", points=curve.points)
    return json.dumps({"curve": curve.points, "rows": rows, "file": str(CURVE_FILE)})


async def _draw_px(path_px: list[tuple[float, float]], px_per_report: float) -> dict:
    """Point at the path's start (verified), then press and trace the planned reports, one per report."""
    W, H = _frame_px()
    if phone.abs_mode:
        return await _draw_px_abs(path_px, px_per_report)
    curve = _load_curve()
    x0, y0 = path_px[0]
    await phone.point(x0 / W * 1000, y0 / H * 1000)
    found = await link.find_pointer(nudge=phone.nudge_units())
    if found is None:
        raise RuntimeError("pointer not found at the stroke's start (is the screen still?)")
    origin = (found[0] * W, found[1] * H)
    plan = plan_path(path_px, curve, px_per_report=px_per_report, max_step=127, origin=origin)
    await link.cmd("pace 127 1")  # every `move` below is exactly one HID report
    t0 = time.monotonic()
    await link.cmd("press")
    try:
        for dx, dy in plan.reports:
            await link.cmd(f"move {dx} {dy}")
    finally:
        await link.cmd("release")
        await link.cmd(f"pace {DEFAULT_PACE}")
    phone._pos = None
    return {"reports": len(plan.reports), "seconds": round(time.monotonic() - t0, 1), "origin_px": [round(v, 1) for v in origin],
            "start_miss_px": round(math.dist(origin, path_px[0]), 1), "predicted_end_px": [round(v, 1) for v in plan.end],
            "target_end_px": [round(v, 1) for v in path_px[-1]]}


async def _draw_px_abs(path_px: list[tuple[float, float]], px_per_report: float) -> dict:
    """Absolute build: the path's points become absolute reports every `px_per_report` px; no planning."""
    W, H = _frame_px()
    x0, y0 = path_px[0]
    await phone.abs_point(x0 / W * 1000, y0 / H * 1000)
    found = await link.find_pointer(nudge=phone.nudge_units())
    origin = (found[0] * W, found[1] * H) if found else (x0, y0)
    pts = []
    for (x1, y1), (x2, y2) in zip(path_px, path_px[1:]):
        n = max(1, int(math.dist((x1, y1), (x2, y2)) / max(0.5, px_per_report)))
        for i in range(1, n + 1):
            px, py = x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n
            pts.append(phone._abs(px / W * 1000, py / H * 1000))
    t0 = time.monotonic()
    await link.cmd("press")
    try:
        await phone.abs_trace(pts, dt_ms=4)
    finally:
        await link.cmd("release")
    phone._pos = None
    return {"reports": len(pts), "seconds": round(time.monotonic() - t0, 1), "origin_px": [round(v, 1) for v in origin],
            "start_miss_px": round(math.dist(origin, path_px[0]), 1), "predicted_end_px": [round(v, 1) for v in path_px[-1]],
            "target_end_px": [round(v, 1) for v in path_px[-1]], "mode": "abs"}


@mcp.tool()
async def draw(points: list[list[float]], space: str = "units", px_per_report: float = 4.0) -> str:
    """Draw a continuous stroke through `points` with the mouse button held: a polyline in screen units
    (0-1000, `space`="units") or mirror pixels ("px"). Uses the measured acceleration curve (measure_curve)
    so the trace follows the path to about a pixel. One HID report per `px_per_report` pixels of path."""
    W, H = _frame_px()
    pts = [(x / 1000 * W, y / 1000 * H) if space == "units" else (x, y) for x, y in points]
    if len(pts) < 2:
        return "refused: give at least two points"
    r = await _draw_px(pts, px_per_report)
    TELEMETRY.event("stroke", **{k: v for k, v in r.items() if k != "origin_px"})
    return json.dumps(r)


def _shape(name: str, size: float, cx: float, cy: float) -> list[tuple[float, float]]:
    h = size / 2
    if name == "line":
        return [(cx - h, cy), (cx + h, cy)]
    if name == "diag":
        return [(cx - h, cy + h), (cx + h, cy - h)]
    if name == "square":
        return [(cx - h, cy - h), (cx + h, cy - h), (cx + h, cy + h), (cx - h, cy + h), (cx - h, cy - h)]
    if name == "circle":
        return [(cx + h * math.cos(2 * math.pi * i / 48), cy + h * math.sin(2 * math.pi * i / 48)) for i in range(49)]
    if name == "zigzag":
        return [(cx - h + size * i / 6, cy + (h if i % 2 else -h) * 0.6) for i in range(7)]
    if name == "signature":  # a loopy wave, like a hand-written flourish
        return [(cx - h + size * t, cy + h * 0.5 * math.sin(6 * math.pi * t) * (1 - 0.6 * t) + h * 0.3 * math.sin(2 * math.pi * t))
                for t in [i / 60 for i in range(61)]]
    raise ValueError(f"unknown shape {name}")


@mcp.tool()
async def stroke_bench(shapes: list[str] = ["line", "diag", "square", "circle", "zigzag", "signature"],
                       size_px: int = 300, px_per_report: float = 4.0, undo: bool = True) -> str:
    """Draw each shape in a blank Freeform board (pen selected), read the ink back from the next frame and
    score it against the intended path: mean deviation (px), p95, coverage (share of the path with ink
    within 2 px) and stray ink. Frames are saved under ~/.esp32-agent/strokes/. Undoes each shape after
    scoring unless undo=false."""
    W, H = _frame_px()
    cx, cy = W / 2, H * 0.45
    STROKES.mkdir(parents=True, exist_ok=True)
    out = []
    for name in shapes:
        path = _shape(name, size_px, cx, cy)
        before = Frame.from_jpeg((await link.frame())[0], scale=1)
        t_board = link.board_now()
        r = await _draw_px(path, px_per_report)
        await link._fresh_frame(t_board + 300, wait_s=6)
        await asyncio.sleep(0.8)
        after = Frame.from_jpeg((await link.frame())[0], scale=1)
        xs = [p[0] for p in path]
        ys = [p[1] for p in path]
        box = (int(min(xs)) - 40, int(min(ys)) - 40, int(max(xs)) + 40, int(max(ys)) + 40)
        was = set(ink_pixels(before.gray, W, H, box=box))
        ink = [p for p in ink_pixels(after.gray, W, H, box=box) if p not in was]
        sc = stroke_score(ink, path)
        end = await link.find_pointer(nudge=phone.nudge_units())  # the pointer's own closure error, app-independent
        sc["pointer_end_miss_px"] = round(math.dist((end[0] * W, end[1] * H), path[-1]), 1) if end else None
        sc["pointer_vs_predicted_px"] = round(math.dist((end[0] * W, end[1] * H), r["predicted_end_px"]), 1) if end else None
        stamp = time.strftime("%H%M%S")
        (STROKES / f"{stamp}-{name}.jpg").write_bytes(after.jpeg)
        row = {"shape": name, **sc, **r}
        out.append(row)
        TELEMETRY.event("stroke", shape=name, **sc, reports=r["reports"], seconds=r["seconds"])
        if undo:
            await link.cmd("key cmd+z")
            await asyncio.sleep(0.5)
    return json.dumps(out)


@mcp.tool()
async def raw(command: str) -> str:
    """Send one raw protocol line to the board (docs/ble-protocol.md): ping, move DX DY, click, scroll V H,
    res W H, fps N, coex bt|balance|wifi, unpair, ... Returns the board's reply. A raw move/home/press leaves
    the tracked pointer position unknown, so the next gesture re-homes first."""
    if command.split(" ", 1)[0] in ("move", "home", "press", "release"):
        phone._pos = None
    return await link.cmd(command, timeout=120)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="ESP32 Agent MCP server")
    ap.add_argument("--http", action="store_true",
                    help="serve streamable HTTP on --host/--port instead of stdio (for the launchd agent: "
                         "macOS only lets a process use Bluetooth if its own app bundle declares it)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ESP32_AGENT_PORT", "8765")))
    a = ap.parse_args()
    if a.http:
        log.info("serving MCP over HTTP at http://%s:%d/mcp", a.host, a.port)
        mcp.run("streamable-http", host=a.host, port=a.port)
    else:
        mcp.run()
