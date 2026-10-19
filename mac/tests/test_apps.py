from pathlib import Path

from apps import AppNotes


def test_unknown_app_and_shortcut_are_none(tmp_path: Path):
    n = AppNotes(tmp_path / "apps.json")
    assert n.shortcut("slack", "cmd+j") is None
    assert n.shortcut("nosuchapp", "cmd+k") is None


def test_learn_persists_and_reloads(tmp_path: Path):
    n = AppNotes(tmp_path / "apps.json")
    n.learn("Slack", "cmd+f", True)
    n.learn("slack", "cmd+j", False)
    m = AppNotes(tmp_path / "apps.json")
    assert m.shortcut("SLACK", "cmd+f") is True and m.shortcut("slack", "cmd+j") is False
    assert m.known() == ["slack"]  # the only app with notes beyond the seed


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
