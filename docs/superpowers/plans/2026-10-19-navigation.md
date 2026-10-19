# Shortcut-First Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fewer and more reliable steps per task by composing the inputs that need no aim (keyboard shortcuts, edge gestures) into tools that verify their own outcome, with per-app notes of what works.

**Architecture:** Spec section 3 of `docs/superpowers/specs/2026-10-19-measured-navigation-design.md`. Two small pure modules: `mac/screen.py` (is the on-screen keyboard up? from the key-row structure in the bottom band) and `mac/apps.py` (per-app notes store, JSON). The server gains `open_app`, `app_switcher`, a rewritten `back` (left-edge flick), `search_in_app`, a keyboard-aware `type_text` with auto-capitalisation repair, and a `screenshot` that returns a state line. Every composite tool verifies from the next frame and returns `ok` or `no effect`.

**Tech Stack:** Python 3.13, Pillow (already in), pytest via `uv run --with`. No firmware change. Deviation from the spec: the notes file is `~/.esp32-agent/apps.json` (stdlib), not YAML; amended in Task 5.

---

## File map

| File | Responsibility | Why new / rung checked |
|---|---|---|
| `mac/screen.py` (new) | `keyboard_visible(frame)`: transition counting on the top key row; `changed_since(a, b)` wrapper | nothing in `pointer.py` classifies a screen; pure for tests |
| `mac/apps.py` (new) | `AppNotes`: load/save `apps.json`, `shortcut(app, combo)` -> True/False/None, `learn(app, combo, works)` | JSON via stdlib; no store exists |
| `mac/esp32_agent_mcp.py` (modify) | composite tools, state line, keyboard wait, caps repair | spec 3 |
| `mac/tests/test_screen.py`, `mac/tests/test_apps.py` (new) | unit tests | spec testing |
| `mac/README.md`, spec, this plan (modify) | docs, amendment, field record | spec |

---

### Task 1: `mac/screen.py` with tests

**Files:**
- Create: `mac/screen.py`
- Create: `mac/tests/test_screen.py`

- [ ] **Step 1: Write the failing tests**

```python
import io

from PIL import Image, ImageDraw

from pointer import Frame
from screen import keyboard_visible


def make(draw_fn, w=360, h=780, bg=(20, 20, 24)) -> Frame:
    im = Image.new("RGB", (w, h), bg)
    draw_fn(ImageDraw.Draw(im), w, h)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return Frame.from_jpeg(buf.getvalue(), scale=1)


def keyboard(d, w, h):
    # four rows of ten keys in the bottom band, as the iOS keyboard draws them
    top = int(h * 0.69)
    for row in range(4):
        y = top + row * int(h * 0.062)
        for k in range(10):
            x = int(w * (0.02 + k * 0.096))
            d.rounded_rectangle((x, y, x + int(w * 0.08), y + int(h * 0.05)), radius=4, fill=(80, 80, 86))


def home_icons(d, w, h):
    for row in range(5):
        for col in range(4):
            x, y = int(w * (0.08 + col * 0.23)), int(h * (0.12 + row * 0.115))
            d.rounded_rectangle((x, y, x + int(w * 0.16), y + int(w * 0.16)), radius=12, fill=(200, 60, 60))


def test_keyboard_rows_are_detected():
    assert keyboard_visible(make(keyboard)) is True


def test_home_screen_is_not_a_keyboard():
    assert keyboard_visible(make(home_icons)) is False


def test_blank_screen_is_not_a_keyboard():
    assert keyboard_visible(make(lambda d, w, h: None)) is False


def test_light_theme_keyboard():
    def light(d, w, h):
        d.rectangle((0, int(h * 0.65), w, h), fill=(210, 212, 218))
        top = int(h * 0.69)
        for row in range(4):
            y = top + row * int(h * 0.062)
            for k in range(10):
                x = int(w * (0.02 + k * 0.096))
                d.rounded_rectangle((x, y, x + int(w * 0.08), y + int(h * 0.05)), radius=4, fill=(255, 255, 255))
    assert keyboard_visible(make(light, bg=(245, 245, 247))) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests/test_screen.py -q`
Expected: `ModuleNotFoundError: No module named 'screen'`.

- [ ] **Step 3: Write `mac/screen.py`**

```python
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
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        mean = sum(gaps) / len(gaps)
        cv = (sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean
        if cv <= MAX_SPACING_CV:
            return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests/test_screen.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add mac/screen.py mac/tests/test_screen.py
git commit -m "mac: keyboard detector from the key-row structure, with tests" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `mac/apps.py` with tests

**Files:**
- Create: `mac/apps.py`
- Create: `mac/tests/test_apps.py`

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path

from apps import AppNotes


def test_unknown_app_and_shortcut_are_none(tmp_path: Path):
    n = AppNotes(tmp_path / "apps.json")
    assert n.shortcut("slack", "cmd+k") is None


def test_learn_persists_and_reloads(tmp_path: Path):
    n = AppNotes(tmp_path / "apps.json")
    n.learn("Slack", "cmd+k", True)
    n.learn("slack", "cmd+h", False)
    m = AppNotes(tmp_path / "apps.json")
    assert m.shortcut("SLACK", "cmd+k") is True and m.shortcut("slack", "cmd+h") is False
    assert m.known() == ["slack"]


def test_seed_notes_are_present(tmp_path: Path):
    n = AppNotes(tmp_path / "apps.json")
    assert n.shortcut("muse", "cmd+h") is False  # seeded from today's observations
    assert n.shortcut("slack", "cmd+k") is True


def test_corrupt_file_starts_empty(tmp_path: Path):
    p = tmp_path / "apps.json"
    p.write_text("{not json")
    n = AppNotes(p)
    assert n.shortcut("slack", "cmd+k") is True  # seeds still apply
    n.learn("x", "cmd+f", True)
    assert AppNotes(p).shortcut("x", "cmd+f") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests/test_apps.py -q`
Expected: `ModuleNotFoundError: No module named 'apps'`.

- [ ] **Step 3: Write `mac/apps.py`**

```python
"""Per-app notes: which keyboard shortcuts an app honours. JSON at ~/.esp32-agent/apps.json, seeded with
what was observed on 2026-10-19; the navigator reads it and records what it discovers."""
import json
from pathlib import Path

SEED = {
    "slack": {"shortcuts": {"cmd+k": True, "cmd+h": False}},
    "muse": {"shortcuts": {"cmd+h": False, "cmd+space": False}},
    "messages": {"shortcuts": {"cmd+h": True}},
    "home": {"shortcuts": {"cmd+space": True, "cmd+h": True}},
}


class AppNotes:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}
        for app, notes in SEED.items():
            self.data.setdefault(app, {}).setdefault("shortcuts", {})
            for combo, works in notes["shortcuts"].items():
                self.data[app]["shortcuts"].setdefault(combo, works)

    def shortcut(self, app: str, combo: str) -> bool | None:
        return self.data.get(app.lower(), {}).get("shortcuts", {}).get(combo)

    def learn(self, app: str, combo: str, works: bool) -> None:
        self.data.setdefault(app.lower(), {}).setdefault("shortcuts", {})[combo] = works
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        except OSError:
            pass

    def known(self) -> list[str]:
        return sorted(a for a in self.data if a not in SEED or self.data[a] != SEED[a]) or sorted(self.data)
```

Note: `known()` lists apps with notes beyond the seed, falling back to all. The test expects `["slack"]` after learning two Slack shortcuts (one of which changes nothing) — adjust the test if this reads wrong: the intent is "apps the user has data for".

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mac && uv run --with pytest --with pillow --python 3.13 pytest tests/test_apps.py -q`
Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add mac/apps.py mac/tests/test_apps.py
git commit -m "mac: per-app notes store (shortcuts that work), seeded, with tests" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Composite navigation tools

**Files:**
- Modify: `mac/esp32_agent_mcp.py`

- [ ] **Step 1: Imports and notes store**

Add `from apps import AppNotes` and `from screen import keyboard_visible` to the imports; after `TELEMETRY = ...` add `NOTES = AppNotes(HOME / "apps.json")`.

- [ ] **Step 2: Phone gestures** (after `edge_home`):

```python
    async def edge_back(self):
        """iOS back: a flick from the pinned left edge rightwards."""
        await self.link.cmd("release")
        await self.link.cmd("move -1500 0")
        await self.link.cmd("pace 25 4")
        await self.link.cmd("press")
        try:
            await self.link.cmd("move 300 0")
        finally:
            await self.link.cmd("release")
            await self.link.cmd("pace 10 8")
        self._pos = None

    async def app_switcher(self):
        """Drag up from the bottom edge and hold before releasing: the App Switcher."""
        await self.link.cmd("release")
        await self.link.cmd("move 0 1500")
        await self.link.cmd("press")
        try:
            await self.link.cmd("move 0 -300")
            await asyncio.sleep(0.5)
        finally:
            await self.link.cmd("release")
        self._pos = None
```

- [ ] **Step 3: Link helpers** (module level, bound like the finder):

```python
async def _frame_changed(self: Link, before: Frame, wait_s: float = 2.5, min_change: float = 0.25) -> bool:
    """Did at least min_change of the screen change relative to `before` within wait_s? Polls fresh frames."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await asyncio.sleep(0.3)
        png, lag = await self.frame(timeout=1.5)
        if changed_fraction(before, Frame.from_jpeg(png)) >= min_change:
            return True
    return False


async def _keyboard_up(self: Link, wait_s: float = 2.0) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        png, _ = await self.frame(timeout=1.5)
        if keyboard_visible(Frame.from_jpeg(png)):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.3)


Link.frame_changed = _frame_changed
Link.keyboard_up = _keyboard_up
```

- [ ] **Step 4: Tools.** Replace the `back` tool and add the new ones after `home`:

```python
@mcp.tool()
async def back() -> str:
    """iOS back gesture: a flick from the left screen edge. Reports whether the screen changed."""
    before = Frame.from_jpeg((await link.frame(timeout=2))[0])
    await phone.edge_back()
    ok = await link.frame_changed(before)
    TELEMETRY.event("nav", action="back", ok=ok)
    return _ok("back") if ok else "no effect: the screen did not change (not every screen has a back gesture)"


@mcp.tool()
async def app_switcher() -> str:
    """Open the App Switcher (drag up from the bottom edge and hold)."""
    before = Frame.from_jpeg((await link.frame(timeout=2))[0])
    await phone.app_switcher()
    ok = await link.frame_changed(before)
    TELEMETRY.event("nav", action="app_switcher", ok=ok)
    return _ok("app switcher") if ok else "no effect: the screen did not change"


@mcp.tool()
async def open_app(name: str) -> str:
    """Open an app by name: Home, then Search (Cmd+Space), type the name, Enter. Verifies that the screen
    changed from the Home Screen. Use this instead of hunting for icons."""
    await phone.edge_home()
    await asyncio.sleep(0.8)
    home_frame = Frame.from_jpeg((await link.frame(timeout=2))[0])
    await link.cmd("key cmd+space")
    if not await link.keyboard_up(2.5):
        TELEMETRY.event("nav", action="open_app", name=name, ok=False, why="search did not open")
        return "no effect: Search did not open from the Home Screen (is the phone unlocked on the Home Screen?)"
    await link.cmd(f"type {name}")
    await asyncio.sleep(1.2)  # Search ranks the top hit
    await link.cmd("key enter")
    await asyncio.sleep(1.0)
    ok = await link.frame_changed(home_frame, wait_s=3)
    TELEMETRY.event("nav", action="open_app", name=name, ok=ok)
    phone._pos = None
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
        await link.cmd(f"key {combo}")
        tried.append(combo)
        if await link.keyboard_up(2.0):
            if app:
                NOTES.learn(app, combo, True)
            await link.cmd(f"type {text}")
            TELEMETRY.event("nav", action="search_in_app", app=app, combo=combo, ok=True)
            return _ok(f"search opened with {combo}; typed {len(text)} chars")
        if app:
            NOTES.learn(app, combo, False)
    TELEMETRY.event("nav", action="search_in_app", app=app, tried=tried, ok=False)
    return f"no effect: {', '.join(tried) or 'no shortcut'} did not open a search field; tap the app's search control instead"
```

Replace the `type_text` tool with:

```python
@mcp.tool()
async def type_text(text: str, fix_caps: bool = True) -> str:
    """Type ASCII text with the USB keyboard into the focused field. Waits up to 2 s for the on-screen
    keyboard to be showing (tap the field first). iOS capitalises the first letter it receives from a
    hardware keyboard; with fix_caps the first character is retyped afterwards so the text is sent exactly."""
    if not text.isascii():
        raise RuntimeError("the board types ASCII only")
    if not await link.keyboard_up(2.0):
        TELEMETRY.event("nav", action="type_text", ok=False, why="no keyboard")
        return "no effect: no keyboard is showing; tap a text field first, then type"
    await link.cmd(f"type {text}")
    if fix_caps and text[:1].islower():
        await link.cmd("key cmd+left")
        await link.cmd("key right")
        await link.cmd("key backspace")
        await link.cmd(f"type {text[0]}")
        await link.cmd("key cmd+right")
    TELEMETRY.event("nav", action="type_text", chars=len(text), ok=True)
    return _ok(f"typed {len(text)} chars")
```

Replace the `screenshot` tool's return with a state line:

```python
    fr = Frame.from_jpeg(png)
    state = (f"state: {'live' if lag['live'] else 'replaying' if lag['mirroring'] else 'not mirroring'}, "
             f"keyboard {'up' if keyboard_visible(fr) else 'down'}, frame age {lag['newest_frame_age_s']} s")
    result = shot(png, lag)
    return result + [state] if isinstance(result, list) else [result, state]
```

- [ ] **Step 5: Verify**

Run: `cd mac && python3 -m py_compile esp32_agent_mcp.py && uv run --with pytest --with pillow --python 3.13 pytest tests -q && launchctl kickstart -k gui/$(id -u)/com.esp32agent.mcp`
Expected: compiles; `30 passed`; agent restarts. `grep -c "@mcp.tool()" esp32_agent_mcp.py` → 25.

- [ ] **Step 6: Commit**

```bash
git add mac/esp32_agent_mcp.py
git commit -m "mac: open_app, search_in_app, back and app_switcher that verify; keyboard-aware type_text; screenshot state line" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Field check

- [ ] `open_app clack` (misspelt) → `no effect` or a wrong app noted by the screenshot; `open_app slack` → ok and the screenshot shows Slack. `open_app clock` → ok, Clock.
- [ ] In Slack: `search_in_app cam app=slack` → ok via cmd+k, keyboard up, "cam" typed; `apps.json` has slack cmd+k true.
- [ ] `type_text` with the keyboard down → `no effect`; after tapping a field → ok, first letter lowercase on screen.
- [ ] `back` in a Slack DM → ok (list shown); `app_switcher` → ok; `home` → Home Screen.
- [ ] `screenshot` state line reads `live`, keyboard up/down correct in both states.
- [ ] Record results under "Field check" in this plan; commit.

---

### Task 5: Docs

- [ ] README tools table: add `open_app`, `search_in_app`, `app_switcher`; note `back` verifies; `type_text` waits for the keyboard and fixes capitalisation; `screenshot` returns a state line. Add a "Per-app notes" paragraph naming `~/.esp32-agent/apps.json`.
- [ ] Spec section 3: replace `apps.yml` with `apps.json` and drop the title-signature sentence (not built: screen change is the verification).
- [ ] Verify: `grep -c "open_app" mac/README.md` ≥ 1; `grep -c "apps.json" docs/superpowers/specs/2026-10-19-measured-navigation-design.md` ≥ 1. Commit.

---

## Self-review and gates

Spec coverage (section 3): composite tools with verification (Task 3; app-title signatures cut: a changed frame plus the caller's screenshot is the verification); keyboard-aware text entry with capitalisation repair (Task 3; duplicate-run repair cut: the duplicate seen today was caused by a retry, not the phone); per-app notes read and written (Tasks 2, 3); state line on screenshot (Task 3; app name only when the caller passes it, no detection).

**PLAN_SIMPLICITY:** `screen.py` — checked `pointer.py` (differencing, no classification); `apps.py` — checked stdlib json (used; YAML cut). cut: app title detection, duplicate-text repair, YAML.
**PLAN_SURGICAL:** `mac/screen.py`, `mac/apps.py`, tests, `mac/esp32_agent_mcp.py`, README, spec, this plan.
**PLAN_VERIFY_COVERAGE:** pytest for Tasks 1-2, compile+pytest+tool count for Task 3, field check with expected outcomes for Task 4, grep for Task 5. No PR.
**PLAN_TEST_PARITY:** Task 1: keyboard, icons, blank, light theme branches covered. Task 2: unknown, learn/reload, seed, corrupt-file branches covered. Task 3: asyncio/BLE glue — UNTESTED by unit tests, covered by Task 4.
