# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2", "bleak>=0.22"]
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
TELEMETRY = Telemetry(HOME / "telemetry.jsonl")
SCAN_TIMEOUT = float(os.environ.get("ESP32_AGENT_SCAN", "15"))
KEEP_AWAKE_S = float(os.environ.get("ESP32_AGENT_KEEPAWAKE", "20"))  # idle seconds between pointer nudges; 0 = off

REHOME_EVERY = 10       # gestures between re-homing (cancels drift of the relative pointer)
# Re-homing: a paced move far past the top-left corner (iOS clamps the pointer there). The board's `home`
# command fires 50 unpaced (-127,-127) reports 2 ms apart, and iOS 26 was seen dropping most of them (with
# the on-screen keyboard up), which left every following tap starting from the wrong place.
HOME_CMD = "move -3000 -3000"
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
        self.video = Reassembler(self.decoder.feed)
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
            await client.start_notify(STATUS, self._on_status)
            self.video = Reassembler(self.decoder.feed)
            await self.decoder.start()
            await client.start_notify(VIDEO, lambda _, d: self.video.feed(bytes(d)))
            self.client, self.name, self.links = client, dev.name, self.links + 1
            log.info("connected to %s (%s), mtu %s", dev.name, dev.address, client.mtu_size)
            TELEMETRY.event("link", state="connected", name=dev.name, mtu=client.mtu_size)
            await self._cmd("stream on")  # keep the mirror flowing into the decoder for as long as we are linked
            if KEEP_AWAKE_S and (self._keeper is None or self._keeper.done()):
                self._keeper = asyncio.create_task(self._keep_awake())

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
        self.link = link
        saved = json.loads(CAL_FILE.read_text()) if CAL_FILE.exists() else None
        # screen fraction = a * mouse units (from the homed corner) + b, per axis; iOS pointer motion has an
        # offset as well as a scale (the first reports after a stop travel less), so one point is not enough
        self.cal = None
        if saved and "ax" in saved:
            self.cal = saved
        elif saved and "w" in saved:
            self.cal = {"ax": 1 / saved["w"], "bx": 0.0, "ay": 1 / saved["h"], "by": 0.0}
        self._pos = None
        self._moves = 0
        self._link = 0

    @property
    def calibrated(self) -> bool:
        return self.cal is not None

    @property
    def span(self) -> dict | None:
        """Mouse units across the whole screen (for display)."""
        return {"w": round(1 / self.cal["ax"]), "h": round(1 / self.cal["ay"])} if self.cal else None

    def save(self):
        HOME.mkdir(parents=True, exist_ok=True)
        CAL_FILE.write_text(json.dumps(self.cal))

    def fit(self, points):
        """points: [[units_x, units_y, screen_x, screen_y], ...] (screen in 0-1000), 2 or more; least squares."""
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
        self.cal = cal
        self.save()
        self._pos = None
        return cal

    def _units(self, t: float, a: str, b: str) -> int:
        return max(0, round((min(max(t, 0), 1000) / 1000 - self.cal[b]) / self.cal[a]))

    def _dx(self, x: float) -> int:
        return self._units(x, "ax", "bx")

    def _dy(self, y: float) -> int:
        return self._units(y, "ay", "by")

    async def _move_to(self, tx: int, ty: int, rehome: bool = True):
        if self._link != self.link.links:  # new link: a dropped gesture may have left the button down
            await self.link.cmd("release")
            self._pos, self._link = None, self.link.links
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            await self.link.cmd(HOME_CMD)
            self._pos, self._moves = (0, 0), 0
        x, y = self._pos
        self._pos = None
        await self.link.cmd(f"move {tx - x} {ty - y}")
        self._pos, self._moves = (tx, ty), self._moves + 1

    async def point(self, x: float, y: float):
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated: calibrate_start, calibrate_move, then calibrate_fit")
        await self._move_to(self._dx(x), self._dy(y))

    async def reset(self):
        self._pos = None
        await self.link.cmd("release")
        await self.link.cmd(HOME_CMD)
        self._pos, self._moves = (0, 0), 0

    async def edge_home(self):
        """The iOS Home gesture from the bottom edge, independent of calibration: pin the pointer to the bottom,
        press, flick up, release; then re-home the tracker."""
        await self.link.cmd("release")
        await self.link.cmd("move 0 1500")  # paced; iOS clamps the pointer at the edge
        await self.link.cmd("press")
        try:
            await self.link.cmd("move 0 -450")
        finally:
            await self.link.cmd("release")
        await asyncio.sleep(0.4)
        await self.reset()

    async def tap(self, x, y):
        await self.point(x, y)
        await self.link.cmd("click")
        await asyncio.sleep(TAP_GAP)

    async def long_press(self, x, y, seconds: float):
        await self.point(x, y)
        await self.link.cmd("press")
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.link.cmd("release")

    async def drag(self, points, hold=True):
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
                       "calibrated": phone.calibrated, "span": phone.span, "hints": hints})


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
    return shot(png, lag)


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
async def set_calibration(span_w: int, span_h: int) -> str:
    """Set a scale-only calibration: mouse units needed to cross the whole screen width and height (no offset;
    calibrate_fit is more accurate)."""
    phone.cal = {"ax": 1 / span_w, "bx": 0.0, "ay": 1 / span_h, "by": 0.0}
    phone.save()
    phone._pos = None
    return "saved"


@mcp.tool()
async def tap(x: int, y: int) -> str:
    """Tap the screen at x, y (0-1000, origin top-left). Aim at the visual center of the element."""
    await phone.tap(x, y)
    return _ok(f"tapped {x},{y}")


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
async def type_text(text: str) -> str:
    """Type ASCII text with the USB keyboard. Tap a text field first (the keyboard must be showing)."""
    if not text.isascii():
        raise RuntimeError("the board types ASCII only")
    await link.cmd(f"type {text}")
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
    """iOS back gesture: a flick from the left screen edge."""
    await phone.swipe(5, 450, 600, 450, hold=False)
    return _ok("back")


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
