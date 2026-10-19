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
