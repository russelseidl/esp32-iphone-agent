# Telemetry and Firmware Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every board and Mac event reconstructible after the fact, and stop the two hardware failures seen today (dropped button releases that wedge the mouse, BLE stalls that force full video replays) without a hand on the phone.

**Architecture:** Spec sections 1 and 5 of `docs/superpowers/specs/2026-10-19-measured-navigation-design.md`. Firmware gets a pure-C retry/rate-limit helper (host-tested), confirmed HID releases with counters in the status JSON, `usbreset` and `pace` commands, a BLE log relay behind a build flag, and a longer video send timeout. The Mac server gets a dependency-free `telemetry.py` (JSONL writer + ring buffer), a `watchdog.py` with a pure stuck-button detector and reconnect backoff, and new tools `diagnostics`, `usb_reset`, `pace`. The AirPlay restart from the spec is reduced to detection and logging (no stop/restart API exists in the ported receiver); the spec is amended in Task 12.

**Tech Stack:** Arduino ESP32 core 3.3.12 (TinyUSB, NimBLE-Arduino 2.5.1), C11 host tests via `make -C hardware/tests`; Python 3.13 server (`mcp>=2`, `bleak`), tests with `pytest` run through `uv`.

Pieces 2, 3 and 4 of the spec (pointer, navigation, bench) are separate plans.

---

## File map

| File | Responsibility | Why new / rung checked |
|---|---|---|
| `hardware/hid_bridge/src/hid/hidretry.h/.c` (new) | Pure C: retry-until-delivered loop, pace bounds, token-bucket rate limiter | Nothing in the repo or libc does retry-with-count or a rate limiter; kept out of the `.ino` so `hardware/tests` can run it |
| `hardware/hid_bridge/boardlog.h/.cpp` (new) | `boardLog(fmt, ...)`: to BLE `log` relay under `LOG_BLE`, to serial under `DEBUG_SERIAL`, rate-limited | The existing `logLine` only serves the AirPlay trace and only to serial |
| `hardware/hid_bridge/hid_bridge.ino` (modify) | confirmed releases, counters, `pace`, `usbreset`, USB event logging, `hid` in status | spec 5 |
| `hardware/hid_bridge/ble_link.cpp` (modify) | `VIDEO_TIMEOUT_MS` 4000, log drops | spec 5, 1 |
| `hardware/hid_bridge/video_link.cpp` (modify) | log GOP overflow / compaction | spec 1 |
| `hardware/flash.sh` (modify) | `--log` flag → `-DLOG_BLE=1` | spec 1 |
| `hardware/tests/test_main.c`, `Makefile` (modify) | tests for hidretry | spec testing |
| `mac/telemetry.py` (new) | JSONL append + in-memory ring, no deps | stdlib `logging` writes text, not one-JSON-per-line with a queryable ring |
| `mac/watchdog.py` (new) | `StuckDetector` (pure), `backoff()` generator | nothing comparable in the server; pure so it is testable |
| `mac/esp32_agent_mcp.py` (modify) | wire telemetry, reconnect loop, `diagnostics`/`usb_reset`/`pace` tools, hid counters in status | spec 1, 5 |
| `mac/tests/test_telemetry.py`, `mac/tests/test_watchdog.py` (new) | unit tests | spec testing |
| `docs/ble-protocol.md`, `mac/README.md`, spec (modify) | document new commands/status fields; amend AirPlay watchdog | spec |

Conventions: firmware C is C11, two-space indent, `snake_case`; `.ino` is Arduino C++. Python: 4-space, type hints, no new third-party deps. Commit after every task with the `Co-Authored-By` trailer the repo uses.

---

### Task 1: hidretry helper (pure C, host-tested)

**Files:**
- Create: `hardware/hid_bridge/src/hid/hidretry.h`
- Create: `hardware/hid_bridge/src/hid/hidretry.c`
- Modify: `hardware/tests/test_main.c` (add tests, call from `main`)
- Modify: `hardware/tests/Makefile` (add the source)

- [ ] **Step 1: Write the failing tests**

Append to `hardware/tests/test_main.c` before `int main(void)`:

```c
#include "../hid_bridge/src/hid/hidretry.h"

static int fake_calls, fake_ok_at, fake_waits;
static bool fake_send(void *ctx) { (void)ctx; fake_calls++; return fake_calls >= fake_ok_at; }
static void fake_wait(int ms) { (void)ms; fake_waits++; }

static void test_hidretry(void) {
  fake_calls = 0; fake_ok_at = 1; fake_waits = 0;
  CHECK(hid_send_confirmed(fake_send, NULL, 5, 20, fake_wait) == 0);  // first try ok: 0 retries, no wait
  CHECK(fake_calls == 1 && fake_waits == 0);
  fake_calls = 0; fake_ok_at = 3; fake_waits = 0;
  CHECK(hid_send_confirmed(fake_send, NULL, 5, 20, fake_wait) == 2);  // ok on the 3rd try: 2 retries
  CHECK(fake_calls == 3 && fake_waits == 2);
  fake_calls = 0; fake_ok_at = 99; fake_waits = 0;
  CHECK(hid_send_confirmed(fake_send, NULL, 5, 20, fake_wait) == -1);  // never: gives up after 5
  CHECK(fake_calls == 5 && fake_waits == 4);

  CHECK(pace_valid(10, 8) && pace_valid(1, 1) && pace_valid(127, 50));
  CHECK(!pace_valid(0, 8) && !pace_valid(128, 8) && !pace_valid(10, 0) && !pace_valid(10, 51));

  ratelimit rl;
  ratelimit_init(&rl, 3, 1000);          // 3 per second
  CHECK(ratelimit_take(&rl, 0) && ratelimit_take(&rl, 10) && ratelimit_take(&rl, 20));
  CHECK(!ratelimit_take(&rl, 30) && rl.dropped == 1);
  CHECK(!ratelimit_take(&rl, 999) && rl.dropped == 2);
  CHECK(ratelimit_take(&rl, 1000) && rl.dropped == 2);  // window rolled: allowed again
}
```

In `main`, add `test_hidretry();` after `test_chunker();`.

In `hardware/tests/Makefile`, change the `test:` line to:

```make
test: test_main.c $(SRC)/bplist/bplist.c $(SRC)/video/gop.c $(SRC)/hid/hidretry.c
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `make -C hardware/tests`
Expected: compile error `hidretry.h: No such file or directory`.

- [ ] **Step 3: Write the implementation**

`hardware/hid_bridge/src/hid/hidretry.h`:

```c
// Small pure-C helpers for the HID path and the board log, so they can be unit-tested on the host.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Calls send(ctx) until it returns true, waiting wait(gap_ms) between tries, at most `tries` times.
// Returns the number of retries (0 = first try succeeded), or -1 if every try failed.
int hid_send_confirmed(bool (*send)(void *), void *ctx, int tries, int gap_ms, void (*wait)(int ms));

// Pointer move pacing: step 1-127 units per report, 1-50 ms between reports.
bool pace_valid(int step, int step_ms);

// Token bucket: at most `max` events per `window_ms`; `dropped` counts refusals.
typedef struct {
  int max, count;
  uint32_t window_ms, window_start;
  uint32_t dropped;
} ratelimit;

void ratelimit_init(ratelimit *r, int max, uint32_t window_ms);
bool ratelimit_take(ratelimit *r, uint32_t now_ms);

#ifdef __cplusplus
}
#endif
```

`hardware/hid_bridge/src/hid/hidretry.c`:

```c
#include "hidretry.h"

int hid_send_confirmed(bool (*send)(void *), void *ctx, int tries, int gap_ms, void (*wait)(int ms)) {
  for (int i = 0; i < tries; i++) {
    if (send(ctx)) return i;
    if (i + 1 < tries) wait(gap_ms);
  }
  return -1;
}

bool pace_valid(int step, int step_ms) {
  return step >= 1 && step <= 127 && step_ms >= 1 && step_ms <= 50;
}

void ratelimit_init(ratelimit *r, int max, uint32_t window_ms) {
  r->max = max;
  r->count = 0;
  r->window_ms = window_ms;
  r->window_start = 0;
  r->dropped = 0;
}

bool ratelimit_take(ratelimit *r, uint32_t now_ms) {
  if (now_ms - r->window_start >= r->window_ms) {
    r->window_start = now_ms;
    r->count = 0;
  }
  if (r->count < r->max) {
    r->count++;
    return true;
  }
  r->dropped++;
  return false;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `make -C hardware/tests`
Expected: `all tests passed`.

- [ ] **Step 5: Commit**

```bash
git add hardware/hid_bridge/src/hid hardware/tests/test_main.c hardware/tests/Makefile
git commit -m "firmware: hidretry helpers (confirmed send, pace bounds, rate limit)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: boardLog with BLE relay and rate limit

**Files:**
- Create: `hardware/hid_bridge/boardlog.h`
- Create: `hardware/hid_bridge/boardlog.cpp`
- Modify: `hardware/flash.sh` (add `--log`)

- [ ] **Step 1: Write boardlog.h**

```cpp
// Board log: one line per event. With -DLOG_BLE=1 lines go to every BLE peer as `log <text>` on the
// reply characteristic (the Mac server writes them to its telemetry); with -DDEBUG_SERIAL=1 they also go
// to USB serial. Rate-limited to LOG_MAX_PER_S lines per second; the number dropped is appended to the next
// line that gets through, as " (+N dropped)".
#pragma once
#include <Arduino.h>

#ifndef LOG_BLE
#define LOG_BLE 0
#endif
const int LOG_MAX_PER_S = 20;

void boardLog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void boardLogLine(const char *msg);   // the airplay_cfg.log callback shape
uint32_t boardLogDropped();
```

- [ ] **Step 2: Write boardlog.cpp**

```cpp
#include "boardlog.h"
#include <stdarg.h>
#include "ble_link.h"
#include "src/hid/hidretry.h"

#ifndef DEBUG_SERIAL
#define DEBUG_SERIAL 0
#endif

static ratelimit limiter;
static bool inited;
static uint32_t reported;  // drops already mentioned in a line

static void emit(const char *msg) {
  if (!inited) {
    ratelimit_init(&limiter, LOG_MAX_PER_S, 1000);
    inited = true;
  }
  if (!ratelimit_take(&limiter, millis())) return;
  char line[200];
  uint32_t unreported = limiter.dropped - reported;
  if (unreported) {
    snprintf(line, sizeof line, "%s (+%lu dropped)", msg, (unsigned long)unreported);
    reported = limiter.dropped;
    msg = line;
  }
#if LOG_BLE
  ble_reply(String("log ") + msg);
#endif
#if DEBUG_SERIAL
  Serial.printf("[%lu] %s\n", millis(), msg);
#endif
}

void boardLog(const char *fmt, ...) {
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  emit(buf);
}

void boardLogLine(const char *msg) { emit(msg); }

uint32_t boardLogDropped() { return inited ? limiter.dropped : 0; }
```

- [ ] **Step 3: Add `--log` to flash.sh**

In `hardware/flash.sh`, in the `for a in "$@"` case list, add before `--build-only`:

```bash
    --log) DEFS+=" -DLOG_BLE=1" ;;
```

and extend the usage comment at the top: `#   --log         send the board log to BLE peers as "log ..." lines (the Mac writes them to telemetry)`.

- [ ] **Step 4: Wire the AirPlay trace through boardLog**

In `hardware/hid_bridge/hid_bridge.ino`: add `#include "boardlog.h"` after `#include "video_link.h"`; delete the `logLine` function; in `startAirplay()` set `cfg.log = boardLogLine;`.

- [ ] **Step 5: Verify it compiles both ways**

Run: `hardware/flash.sh --build-only && hardware/flash.sh --build-only --log --debug`
Expected: both print `Sketch uses ...` with no errors.

- [ ] **Step 6: Commit**

```bash
git add hardware/hid_bridge/boardlog.h hardware/hid_bridge/boardlog.cpp hardware/hid_bridge/hid_bridge.ino hardware/flash.sh
git commit -m "firmware: boardLog over the BLE log relay (flash.sh --log), rate-limited" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Confirmed releases, HID counters, `pace`

**Files:**
- Modify: `hardware/hid_bridge/hid_bridge.ino`

- [ ] **Step 1: Replace the pacing constants and add HID state**

Replace

```cpp
const int STEP = 10;      // max pointer delta per report while moving
const int STEP_MS = 8;    // report interval; constant pace keeps iOS acceleration predictable
```

with

```cpp
#include "src/hid/hidretry.h"
static int STEP = 10;      // max pointer delta per report while moving (`pace` changes it at run time)
static int STEP_MS = 8;    // report interval; constant pace keeps iOS acceleration predictable
const int RELEASE_TRIES = 5, RELEASE_GAP_MS = 20;
static uint32_t hidFail, hidRel;  // reports that never went out; release retries that were needed
```

- [ ] **Step 2: Add the confirmed-release helpers after `usbReady()`**

```cpp
// The core's mouse.release()/keyboard.releaseAll() drop the report silently when the host does not take
// it within 100 ms; iOS then keeps the button held and every later move becomes a drag. Resend the
// all-up report until the HID layer confirms delivery.
static bool sendMouseUp(void *) {
  hid_mouse_report_t r = {.buttons = 0, .x = 0, .y = 0, .wheel = 0, .pan = 0};
  return mouse.sendReport(r);
}
static void hidWait(int ms) { delay(ms); }

static void releaseMouse() {
  mouse.release(MOUSE_LEFT);  // keeps the core's own button state in sync
  int retries = hid_send_confirmed(sendMouseUp, NULL, RELEASE_TRIES, RELEASE_GAP_MS, hidWait);
  if (retries < 0) { hidFail++; boardLog("hid: mouse release not delivered"); }
  else if (retries > 0) { hidRel += retries; boardLog("hid: mouse release after %d retries", retries); }
}

static void releaseKeys() {
  keyboard.releaseAll();           // the core gives no delivery result for keys: send twice, 20 ms apart
  delay(RELEASE_GAP_MS);
  KeyReport none = {};
  keyboard.sendReport(&none);
}
```

- [ ] **Step 3: Use them in `run()` and `pressCombo()` and add `pace`**

In `pressCombo`, replace both `keyboard.releaseAll();` with `releaseKeys();`.

In `run()`, replace the `release` and `click` lines with:

```cpp
  if (cmd == "release") { releaseMouse(); return "ok"; }
  if (cmd == "click") { mouse.press(MOUSE_LEFT); delay(60); releaseMouse(); return "ok"; }
```

In `board()`, before `return run(line);`, add:

```cpp
  if (cmd == "pace") {  // move pacing: STEP units per report, STEP_MS between reports (not stored)
    if (n < 2 || !pace_valid(a, b)) return "err usage: pace STEP(1-127) STEP_MS(1-50)";
    STEP = a;
    STEP_MS = b;
    return "ok";
  }
```

In `loop()`, where the link drops, replace `mouse.release(MOUSE_LEFT);` and `keyboard.releaseAll();` with `releaseMouse();` and `releaseKeys();`.

- [ ] **Step 4: Add the counters to the status JSON**

In `statusJson`, change the full format string's `"ble":{...}}` tail to end with

```
"\"ble\":{\"mtu\":%u,\"phy\":%u,\"kbps\":%lu,\"drop\":%lu,\"rdrop\":%lu,\"n\":%d},"
"\"hid\":{\"fail\":%lu,\"rel\":%lu},\"logdrop\":%lu}"
```

and append the arguments `(unsigned long)hidFail, (unsigned long)hidRel, (unsigned long)boardLogDropped()` after `ble_peers()`. Raise the buffer `char s[384]` to `char s[448]`.

- [ ] **Step 5: Verify**

Run: `hardware/flash.sh --build-only`
Expected: `Sketch uses ...`, no errors. Then `grep -c "releaseMouse" hardware/hid_bridge/hid_bridge.ino` → `5`.

- [ ] **Step 6: Commit**

```bash
git add hardware/hid_bridge/hid_bridge.ino
git commit -m "firmware: confirmed mouse releases, hid counters in status, pace command" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `usbreset` and USB event logging

**Files:**
- Modify: `hardware/hid_bridge/hid_bridge.ino`

- [ ] **Step 1: Add the USB event handler after `usbState()`**

```cpp
static void onUsbEvent(void *, esp_event_base_t base, int32_t id, void *) {
  if (base != ARDUINO_USB_EVENTS) return;
  const char *what = id == ARDUINO_USB_STARTED_EVENT ? "mounted" : id == ARDUINO_USB_STOPPED_EVENT ? "unmounted"
                   : id == ARDUINO_USB_SUSPEND_EVENT ? "suspended" : id == ARDUINO_USB_RESUME_EVENT ? "resumed" : "event";
  boardLog("usb: %s", what);
}
```

In `setup()`, before `USB.begin();`, add `USB.onEvent(onUsbEvent);`.

- [ ] **Step 2: Add `usbreset` to `board()`**

Before the `pace` block:

```cpp
  if (cmd == "usbreset") {  // re-enumerate on the phone, like a re-plug (clears a stuck button on iOS)
    ble_reply(id + " ok");
    boardLog("usb: reset requested");
    tud_disconnect();
    delay(500);
    tud_connect();
    return "";
  }
```

- [ ] **Step 3: Verify**

Run: `hardware/flash.sh --build-only`
Expected: builds. `grep -n "tud_disconnect\|USB.onEvent" hardware/hid_bridge/hid_bridge.ino` shows both.

- [ ] **Step 4: Commit**

```bash
git add hardware/hid_bridge/hid_bridge.ino
git commit -m "firmware: usbreset command and USB mount/suspend logging" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Longer video timeout, log BLE drops and GOP events

**Files:**
- Modify: `hardware/hid_bridge/ble_link.cpp`
- Modify: `hardware/hid_bridge/video_link.cpp`

- [ ] **Step 1: ble_link.cpp**

Change `const uint32_t VIDEO_TIMEOUT_MS = 1500;` to `const uint32_t VIDEO_TIMEOUT_MS = 4000;  // a stalled link pauses the stream; 1.5 s forced a full replay on every hiccup`.

Add `#include "boardlog.h"` after `#include "ble_link.h"`. In `ble_video_send`, after `drops++;` add `boardLog("ble: video notify dropped (%lu total)", (unsigned long)drops);`. In the `onDisconnect` callback (the line with `BLE_LOG("disconnect conn ...`), add `boardLog("ble: disconnect reason 0x%x", reason);`.

- [ ] **Step 2: video_link.cpp**

Add `#include "boardlog.h"`. In `video_on_au`, after `gop_push(...)` and before `xSemaphoreGive(lock)`, capture and compare:

```cpp
  static uint32_t seenOverflows, seenCont;
  gop_push(&gop, au, len, idr, now);
  bool overflowed = gop.overflows != seenOverflows, continued = gop.continuations != seenCont;
  seenOverflows = gop.overflows;
  seenCont = gop.continuations;
  xSemaphoreGive(lock);
  if (overflowed) boardLog("gop: overflow, dropped until the next IDR");
  if (continued) boardLog("gop: compacted, continuing for the streaming client (%u AUs kept)", gop.n);
```

(replace the existing `gop_push(...)` line and the `xSemaphoreGive(lock);` that follows it.)

- [ ] **Step 3: Verify**

Run: `hardware/flash.sh --build-only --log && make -C hardware/tests`
Expected: builds; `all tests passed`.

- [ ] **Step 4: Commit**

```bash
git add hardware/hid_bridge/ble_link.cpp hardware/hid_bridge/video_link.cpp
git commit -m "firmware: 4 s video send timeout; log BLE drops and GOP overflow/compaction" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: AirPlay stall detection (log only)

**Files:**
- Modify: `hardware/hid_bridge/hid_bridge.ino`

- [ ] **Step 1: Add the check to `statusTask`**

Inside the `for (;;)` loop, after `if (!ble_connected()) continue;`:

```cpp
    static uint32_t idleSince;  // station on the hotspot but no mirroring session
    bool stalled = net_peers() > 0 && airplay_state() == AIRPLAY_IDLE;
    if (!stalled) idleSince = 0;
    else if (!idleSince) idleSince = millis();
    else if (millis() - idleSince > 30000) { boardLog("airplay: no session for 30 s with a station present"); idleSince = millis(); }
```

- [ ] **Step 2: Verify**

Run: `hardware/flash.sh --build-only`
Expected: builds.

- [ ] **Step 3: Commit**

```bash
git add hardware/hid_bridge/hid_bridge.ino
git commit -m "firmware: log an AirPlay receiver stall (station present, no session for 30 s)" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: `mac/telemetry.py` with tests

**Files:**
- Create: `mac/telemetry.py`
- Create: `mac/tests/__init__.py` (empty)
- Create: `mac/tests/test_telemetry.py`

- [ ] **Step 1: Write the failing tests**

`mac/tests/test_telemetry.py`:

```python
import json
from pathlib import Path

from telemetry import Telemetry


def test_event_is_appended_as_one_json_line(tmp_path: Path):
    t = Telemetry(tmp_path / "t.jsonl", ring=3)
    t.event("cmd", line="ping", reply="ok", ms=12.5)
    rows = (tmp_path / "t.jsonl").read_text().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["kind"] == "cmd" and row["line"] == "ping" and row["ms"] == 12.5
    assert isinstance(row["t"], float)


def test_ring_keeps_only_the_last_n(tmp_path: Path):
    t = Telemetry(tmp_path / "t.jsonl", ring=3)
    for i in range(5):
        t.event("link", n=i)
    assert [e["n"] for e in t.recent()] == [2, 3, 4]
    assert [e["n"] for e in t.recent(2)] == [3, 4]


def test_counters_accumulate(tmp_path: Path):
    t = Telemetry(tmp_path / "t.jsonl")
    t.count("replays")
    t.count("replays")
    t.count("drops", 3)
    assert t.counters == {"replays": 2, "drops": 3}


def test_write_failure_does_not_raise(tmp_path: Path):
    t = Telemetry(tmp_path / "missing-dir" / "t.jsonl", mkdir=False)
    t.event("cmd", line="ping")  # directory does not exist: swallowed, event still in the ring
    assert t.recent()[0]["line"] == "ping"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac && uv run --with pytest --python 3.13 pytest tests/test_telemetry.py -q`
Expected: `ModuleNotFoundError: No module named 'telemetry'`.

- [ ] **Step 3: Write `mac/telemetry.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac && uv run --with pytest --python 3.13 pytest tests/test_telemetry.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add mac/telemetry.py mac/tests/__init__.py mac/tests/test_telemetry.py
git commit -m "mac: telemetry module (JSONL events, ring, counters) with tests" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `mac/watchdog.py` with tests

**Files:**
- Create: `mac/watchdog.py`
- Create: `mac/tests/test_watchdog.py`

- [ ] **Step 1: Write the failing tests**

```python
from watchdog import StuckDetector, backoff


def test_hid_fail_rising_triggers_once_per_rise():
    d = StuckDetector(miss_units=150)
    assert d.hid(fail=0) is False
    assert d.hid(fail=1) is True      # rose: reset wanted
    assert d.hid(fail=1) is False     # unchanged: no repeat
    assert d.hid(fail=3) is True


def test_two_consecutive_far_landings_trigger():
    d = StuckDetector(miss_units=150)
    assert d.landing(aimed=(500, 500), landed=(510, 495)) is False
    assert d.landing(aimed=(500, 500), landed=(100, 900)) is False  # one miss can be a bad read
    assert d.landing(aimed=(500, 500), landed=(900, 100)) is True   # two in a row
    assert d.landing(aimed=(500, 500), landed=(900, 100)) is False  # reset after triggering


def test_a_good_landing_clears_the_streak():
    d = StuckDetector(miss_units=150)
    d.landing(aimed=(500, 500), landed=(100, 900))
    d.landing(aimed=(500, 500), landed=(505, 505))
    assert d.landing(aimed=(500, 500), landed=(100, 900)) is False


def test_backoff_doubles_and_caps():
    assert list(backoff(1, 15, 6)) == [1, 2, 4, 8, 15, 15]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac && uv run --with pytest --python 3.13 pytest tests/test_watchdog.py -q`
Expected: `ModuleNotFoundError: No module named 'watchdog'`.

- [ ] **Step 3: Write `mac/watchdog.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac && uv run --with pytest --python 3.13 pytest tests -q`
Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add mac/watchdog.py mac/tests/test_watchdog.py
git commit -m "mac: watchdog decision logic (stuck-button detector, backoff) with tests" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Wire telemetry into the server

**Files:**
- Modify: `mac/esp32_agent_mcp.py`

- [ ] **Step 1: Import and construct**

After `from mcp.server.mcpserver import MCPServer, Image` add:

```python
from telemetry import Telemetry
from watchdog import StuckDetector, backoff
```

After `CAL_FILE = HOME / "calibration.json"` add:

```python
TELEMETRY = Telemetry(HOME / "telemetry.jsonl")
```

- [ ] **Step 2: Record commands, board logs, link events**

In `Link._cmd`, after `self.last_cmd_at = time.monotonic()` add:

```python
        TELEMETRY.event("cmd", line=line, reply=reply, ms=round((time.monotonic() - t0) * 1000, 1))
```

and in the `except asyncio.TimeoutError:` branch, before `raise`, add `TELEMETRY.event("cmd", line=line, reply=None, ms=None, timeout=True)`.

In `Link._on_reply`, replace `log.info("[%s] %s", rid, rest)` with:

```python
            log.info("[%s] %s", rid, rest)
            TELEMETRY.event("boardlog", text=rest)
```

In `Link.ensure`, after `log.info("connected to %s ...")` add `TELEMETRY.event("link", state="connected", name=dev.name, mtu=client.mtu_size)`. In `Link._on_disconnect`, after `log.warning("board disconnected")` add `TELEMETRY.event("link", state="disconnected")`.

- [ ] **Step 3: Record frames and replays at screenshot time**

In the `screenshot` tool, replace `return shot(*await link.frame())` with:

```python
    png, lag = await link.frame()
    TELEMETRY.event("frame", live=lag["live"], mirroring=lag["mirroring"], replay_bytes=lag["replay_bytes_to_go"],
                    age_s=lag["newest_frame_age_s"], pending=lag["pending_decodes"])
    if not lag["live"]:
        TELEMETRY.count("stale_screenshots")
    return shot(png, lag)
```

- [ ] **Step 4: Add the `diagnostics` tool** after `status`:

```python
@mcp.tool()
async def diagnostics(n: int = 50) -> str:
    """The last n telemetry events (commands with round-trip times, frames, link changes, board log lines,
    recoveries) and the running counters. Use it to find out what happened when something misbehaved."""
    st = link.status
    return json.dumps({"counters": dict(TELEMETRY.counters), "board": {"hid": st.get("hid"), "ble": st.get("ble"),
                       "logdrop": st.get("logdrop")}, "events": TELEMETRY.recent(n)}, default=str)
```

- [ ] **Step 5: Verify**

Run: `cd mac && python3 -m py_compile esp32_agent_mcp.py && grep -c "TELEMETRY.event" esp32_agent_mcp.py`
Expected: no error; count `6`. Then restart the agent and call the tool over HTTP:

```bash
launchctl kickstart -k gui/$(id -u)/com.esp32agent.mcp; sleep 3
python3 - <<'EOF'
import json, urllib.request
U="http://127.0.0.1:8765/mcp"
def post(b,s=None):
    r=urllib.request.Request(U,data=json.dumps(b).encode(),method="POST",headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream",**({"Mcp-Session-Id":s} if s else {})})
    with urllib.request.urlopen(r,timeout=60) as x: return x.headers.get("Mcp-Session-Id",s),[json.loads(l[5:]) for l in x.read().decode().splitlines() if l.startswith("data:")]
s,_=post({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}})
post({"jsonrpc":"2.0","method":"notifications/initialized"},s)
post({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"status","arguments":{}}},s)
_,m=post({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"diagnostics","arguments":{"n":5}}},s)
print(m[-1]["result"]["content"][0]["text"][:400])
EOF
tail -2 ~/.esp32-agent/telemetry.jsonl
```

Expected: the diagnostics text contains `"events": [` with a `"kind": "cmd"` row for `ping`; the JSONL tail shows the same rows.

- [ ] **Step 6: Commit**

```bash
git add mac/esp32_agent_mcp.py
git commit -m "mac: telemetry events for commands, frames, link and board log; diagnostics tool" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Reconnect loop and stuck-button recovery in the server

**Files:**
- Modify: `mac/esp32_agent_mcp.py`

- [ ] **Step 1: Reconnect with backoff while a session was active**

In `Link.__init__` add `self._reconnector = None` and `self.stuck = StuckDetector()`. In `Link._on_disconnect`, after the telemetry event, add:

```python
        if self._reconnector is None or self._reconnector.done():
            self._reconnector = asyncio.get_running_loop().create_task(self._reconnect())
```

Add the method after `_on_disconnect`:

```python
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
```

Note: `_on_disconnect` is called by bleak on the event loop thread, so `get_running_loop()` is valid there.

- [ ] **Step 2: Recover from a rising hid.fail counter**

In `Link._on_status`, after `self.status_at = time.monotonic()`, add:

```python
            fail = int(self.status.get("hid", {}).get("fail", 0))
            if self.stuck.hid(fail) and self._recovering is None:
                self._recovering = asyncio.get_running_loop().create_task(self.usb_reset("hid.fail rose"))
```

Add `self._recovering = None` to `__init__`, and the method:

```python
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
```

- [ ] **Step 3: Add the `usb_reset` and `pace` tools** after `reset_pointer`:

```python
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
```

- [ ] **Step 4: Verify**

Run: `cd mac && python3 -m py_compile esp32_agent_mcp.py && uv run --with pytest --python 3.13 pytest tests -q`
Expected: compiles; `8 passed`. Restart the agent (`launchctl kickstart -k gui/$(id -u)/com.esp32agent.mcp`), then with the board on the phone call `usb_reset` through the MCP tool and confirm: the board's status `up` keeps counting (no reboot), the phone re-mounts (status `usb` is `ok` within 5 s), and `diagnostics` shows a `recover` event with `action: usbreset`. Requires the flashed firmware from Task 11.

- [ ] **Step 5: Commit**

```bash
git add mac/esp32_agent_mcp.py
git commit -m "mac: reconnect with backoff, usbreset on rising hid.fail, usb_reset and pace tools" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Flash and field-check

**Files:** none (operations).

- [ ] **Step 1: Build the log-enabled firmware and flash it**

With the board on the Mac: `hardware/flash.sh --log`. Expected: `Hash of data verified.` twice and `Hard resetting via RTS pin...`.

- [ ] **Step 2: Field-check on the phone**

Board on the phone, mirroring on. Through the MCP tools:
1. `status` → `status.hid` is `{"fail": 0, "rel": 0}` and `logdrop` is `0`.
2. `raw "pace 5 8"` → `ok`; `raw "pace 0 8"` → `err usage: ...`.
3. Tap a Home Screen icon and press Home (`tap`, `home`) → `diagnostics 20` shows `cmd` rows with `ms` values and, within a second of the Home drag, no `hid: mouse release not delivered` board log.
4. `usb_reset` → within 5 s `status.usb` is `ok` again and a `boardlog` row `usb: mounted` appears in `diagnostics`.
5. Lock the phone for 10 s, unlock, restart mirroring → `diagnostics` shows `usb: suspended`, `usb: resumed`, and the AirPlay session lines.

Record the observed values in `docs/superpowers/plans/2026-10-19-telemetry-and-hardening.md` under a "Field check" heading at the bottom (one line per item: pass/fail and the numbers).

- [ ] **Step 3: Commit the field notes**

```bash
git add docs/superpowers/plans/2026-10-19-telemetry-and-hardening.md
git commit -m "docs: field-check results for telemetry and hardening" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: Documentation and spec amendment

**Files:**
- Modify: `docs/ble-protocol.md`
- Modify: `mac/README.md`
- Modify: `docs/superpowers/specs/2026-10-19-measured-navigation-design.md`

- [ ] **Step 1: ble-protocol.md**

Under `## cmd`, after the `coex` bullet, add:

```markdown
- `pace STEP STEP_MS` — pointer move pacing: 1-127 units per report, 1-50 ms between reports (not stored).
- `usbreset` — replies `ok`, then disconnects and reconnects the USB device so the phone re-enumerates it
  (clears a stuck mouse button).
```

Under `## status`, after the `ble.n` line, add:

```markdown
`hid.fail` = HID reports that never went out (release not confirmed), `hid.rel` = release retries that were
needed, `logdrop` = board log lines dropped by the rate limit.
```

Add a section after `## net (JSON)`:

```markdown
## log (reply characteristic)
A board built with `flash.sh --log` sends `log <text>` lines on `reply` (no request id): HID delivery
problems, USB mount/suspend/resume, AirPlay session steps, GOP overflow/compaction, BLE drops. At most 20 per
second; a line that follows dropped ones ends with ` (+N dropped)`.
```

- [ ] **Step 2: mac/README.md**

In the Tools table add:

```markdown
| `diagnostics` | last N telemetry events (commands, frames, link, board log, recoveries) and counters |
| `usb_reset`, `pace` | re-enumerate the board on the phone; set pointer move pacing |
```

After the "How the picture stays fresh" section add:

```markdown
## Telemetry and self-recovery

Every command, screenshot, link change, board log line and recovery is appended as one JSON line to
`~/.esp32-agent/telemetry.jsonl` (`diagnostics` shows the latest). Flash with `hardware/flash.sh --log` to
get the board's own log in there too. The server reconnects on its own after a BLE drop (1 s doubling to
15 s), and when the board reports an undelivered mouse release (`status.hid.fail` rising) it sends `usbreset`,
which re-enumerates the device like a re-plug; the pointer re-homes on the next gesture.
```

- [ ] **Step 3: Amend the spec**

In the spec's section 5, replace the AirPlay watchdog bullet with:

```markdown
- AirPlay stall detection: if a station is on the hotspot but no mirroring session for 30 s, log it (once
  per 30 s). The ported receiver has no stop/restart API; a restart is deferred until telemetry shows stalls
  that a phone-side mirroring restart does not explain.
```

- [ ] **Step 4: Verify**

Run: `grep -c "usbreset" docs/ble-protocol.md mac/README.md` → `2` and `1`; `grep -c "stall detection" docs/superpowers/specs/2026-10-19-measured-navigation-design.md` → `1`.

- [ ] **Step 5: Commit**

```bash
git add docs/ble-protocol.md mac/README.md docs/superpowers/specs/2026-10-19-measured-navigation-design.md
git commit -m "docs: pace, usbreset, hid counters, board log relay, telemetry; amend AirPlay watchdog" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review notes

Spec coverage (sections 1 and 5): board log relay with rate limit and `LOG_BLE` (Tasks 2, 5, 6); `hid.fail`/`hid.rel` in status (Task 3); JSONL telemetry with kinds cmd/frame/boardlog/link/replay/recover (Tasks 7, 9, 10; `replay` is carried on the `frame` rows as `replay_bytes`, `trial` belongs to the bench plan); `diagnostics` (Task 9); confirmed releases and double key release (Task 3); `usbreset` (Task 4); `pace` (Task 3); AirPlay watchdog reduced to detection (Task 6, amended in Task 12); `VIDEO_TIMEOUT_MS` 4000 (Task 5); stuck-button detector by `hid.fail` (Task 10) — the landing-based trigger needs verified taps and ships with the pointer plan, where `StuckDetector.landing` (already tested here) is called; BLE reconnect backoff (Task 10); blind mirroring macro and locked-phone reporting belong to the navigation plan (needs edge gestures and `apps.yml`). Testing bullets: hidretry and pace bounds host-tested (Task 1); telemetry and detector unit-tested (Tasks 7, 8); the HID seam is `hid_send_confirmed` with a fake sender.

## Discipline gates

**PLAN_SIMPLICITY:** `src/hid/hidretry.c` — checked libc and the repo's gop/bplist helpers, insufficient: no retry-with-count or token bucket exists, and the `.ino` cannot be host-tested. `boardlog.cpp` — checked `logLine`/`BLE_LOG`, insufficient: serial-only and unrate-limited. `mac/telemetry.py` — checked `logging`, insufficient: text lines, no ring/counters for a tool to return. `mac/watchdog.py` — checked the server, insufficient: no recovery logic exists and it must be unit-tested without BLE. No new dependencies (pytest is a dev-only `uv run --with`). cut: the AirPlay restart (no API), the `trial` event kind (bench plan).

**PLAN_SURGICAL:** `src/hid/hidretry.{h,c}` (spec 5 confirmed releases, pace bounds, spec 1 rate limit); `boardlog.{h,cpp}` (spec 1 relay); `hid_bridge.ino` (spec 5 releases, counters, pace, usbreset, USB events, stall log); `ble_link.cpp` (spec 5 timeout, spec 1 drop log); `video_link.cpp` (spec 1 GOP log); `flash.sh` (spec 1 `--log`); `tests/test_main.c`, `Makefile` (spec testing); `mac/telemetry.py`, `mac/watchdog.py`, `mac/tests/*` (spec 1, 5, testing); `mac/esp32_agent_mcp.py` (spec 1 wiring, spec 5 recovery and tools); `docs/ble-protocol.md`, `mac/README.md` (document new protocol and tools); the spec file (amendment).

**PLAN_VERIFY_COVERAGE:** every task has a runnable verify (make/pytest/build/grep/HTTP call/field check). No PR is created by this plan (commits on the local branch), so no review task is required.

**PLAN_TEST_PARITY:** Task 1: all branches of `hid_send_confirmed`, `pace_valid`, `ratelimit_take` have cases. Task 7: ring, counters, write-failure branches covered. Task 8: hid rise/no-rise, streak/clear, backoff cap covered. Task 3: `releaseMouse` retries<0 / >0 / ==0 branches — UNTESTED on host because they run on the Arduino core; exercised by the Task 11 field check (log lines and counters). Task 4/5/6: firmware-only logging branches — UNTESTED on host for the same reason, covered by Task 11 field check. Task 9/10: telemetry wiring and reconnect loop — UNTESTED by unit tests (asyncio + bleak), verified by the Task 9/10 HTTP checks.

## Field check (2026-10-19, firmware af68ffa + mac 4ebab2d..34b4ee3, board ESP32 Agent 57CD, iPhone18,1 on iOS 26)

1. `status` → `hid: {fail: 0, rel: 0}`, `logdrop: 0`. PASS.
2. `pace 5 8` → `ok`; `pace 0 8` → `err usage: pace STEP(1-127) STEP_MS(1-50)` (reply captured in telemetry, 27.7 ms). PASS.
3. Tap on the Clock icon opened Clock; `home` (edge drag) returned to the Home Screen with the pointer homed.
   Command round trips: move 29-203 ms, click 119 ms, release 87-160 ms, paced corner move 2.6-2.9 s. No
   `hid: mouse release not delivered` lines; counters stayed 0/0. PASS.
4. `usbreset` (board on the Mac) → board log `usb: reset requested`, `usb: suspended`, `usb: mounted` within
   1 s; the device re-enumerated with the same serial name. PASS. The BLE reconnect loop also recovered the
   link on its own after the flash reboot (`recover reconnect ok after 1 s`).
5. Lock 10 s / unlock / restart mirroring → board log shows `teardown (video)`, `teardown (session)`,
   `rtsp: connection closed`, then the full pair/fp-setup/mirror handshake on restart. No `usb: suspended`
   within a 10 s lock: iOS keeps the HID link awake for a while after locking (the suspend seen earlier in the
   day followed a multi-minute lock). PASS with that note. Also observed: iOS re-sent SPS/PPS twice during a
   session (13:06:22, 13:06:27), so keyframes are not strictly once per session.
