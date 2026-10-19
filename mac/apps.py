"""Per-app notes: which keyboard shortcuts an app honours. JSON at ~/.esp32-agent/apps.json, seeded with
what was observed on 2026-10-19; the navigator reads it and records what it discovers."""
import json
from pathlib import Path

SEED = {
    "slack": {"shortcuts": {"cmd+h": False}},
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
        """Apps with notes beyond the seed (all apps if none)."""
        extra = sorted(a for a in self.data if a not in SEED or self.data[a] != SEED[a])
        return extra or sorted(self.data)
