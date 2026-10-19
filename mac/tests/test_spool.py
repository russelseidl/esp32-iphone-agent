import json
import time

from spool import CONFIG, IDR, HDR, Spool

CFG = b"\x00\x00\x00\x01\x67cfg\x00\x00\x00\x01\x68pps"
KEY = b"\x00\x00\x00\x01\x65" + b"k" * 50
P = b"\x00\x00\x00\x01\x41" + b"p" * 10


def test_records_from_the_keyframe_and_reloads(tmp_path):
    s = Spool(tmp_path / "gop.bin")
    s.record(P, 0, 5)  # before any keyframe: nothing usable
    assert not (tmp_path / "gop.bin").exists()
    s.record(CFG, CONFIG, 9)
    s.record(KEY, IDR, 10)
    s.record(P, 0, 11)
    s.record(P, 0, 12)
    assert (s.bytes, s.last_ts, s.n) == (len(KEY) + 2 * len(P), 12, 3)

    s2 = Spool(tmp_path / "gop.bin")
    got = s2.load()
    assert [(f, ts) for _, f, ts in got] == [(CONFIG, 9), (IDR, 10), (0, 11), (0, 12)]
    assert got[0][0] == CFG and got[1][0] == KEY
    assert (s2.bytes, s2.last_ts, s2.n) == (s.bytes, 12, 3)
    s2.record(P, 0, 13)  # appends after a load
    assert len(Spool(tmp_path / "gop.bin").load()) == 5


def test_new_keyframe_restarts_and_continuation_config_appends(tmp_path):
    s = Spool(tmp_path / "gop.bin")
    s.record(CFG, CONFIG, 1)
    s.record(KEY, IDR, 2)
    s.record(P, 0, 3)
    s.record(CFG, CONFIG, 4)  # a board continuation resends the config: keep going
    s.record(P, 0, 5)
    assert s.n == 3 and s.last_ts == 5
    s.record(KEY, IDR, 6)  # a new mirroring session: start over
    assert s.n == 1 and s.bytes == len(KEY)
    assert [ts for _, _, ts in Spool(tmp_path / "gop.bin").load()] == [4, 6]


def test_torn_or_foreign_files_are_dropped(tmp_path):
    p = tmp_path / "gop.bin"
    s = Spool(p)
    s.record(CFG, CONFIG, 1)
    s.record(KEY, IDR, 2)
    s.record(P, 0, 3)
    with open(p, "ab") as f:
        f.write(HDR.pack(0, 4, 99) + b"half")  # the agent died mid-write
    got = Spool(p).load()
    assert [ts for _, _, ts in got] == [1, 2, 3]
    assert p.stat().st_size == sum(HDR.size + len(a) for a, _, _ in got)  # the torn record was cut off

    p.write_bytes(HDR.pack(0, 7, len(P)) + P)  # no config/keyframe at the start
    assert Spool(p).load() == [] and not p.exists()


def test_same_boot_uses_the_sidecar(tmp_path):
    s = Spool(tmp_path / "gop.bin")
    s.record(CFG, CONFIG, 1)
    s.record(KEY, IDR, 2)
    s.note_board(1_000_000)
    s._noted_at = 0  # force the sidecar write
    s.record(P, 0, 3)
    d = json.loads((tmp_path / "gop.json").read_text())
    assert d["up"] == 1_000_000 and abs(d["wall"] - time.time()) < 5 and d["last_ts"] == 3
    assert Spool(tmp_path / "gop.bin").same_boot(1_000_000 + 1000)
    assert not Spool(tmp_path / "gop.bin").same_boot(5_000)  # rebooted: timestamps restarted
    s.clear()
    assert not Spool(tmp_path / "gop.bin").same_boot(1_000_000)


def test_cap_stops_recording(tmp_path, monkeypatch):
    import spool
    monkeypatch.setattr(spool, "MAX_BYTES", len(KEY) + len(P))
    s = Spool(tmp_path / "gop.bin")
    s.record(CFG, CONFIG, 1)
    s.record(KEY, IDR, 2)
    s.record(P, 0, 3)
    s.record(P, 0, 4)
    assert s.full and s.n == 2 and s.last_ts == 3
