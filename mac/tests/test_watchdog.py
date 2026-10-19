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
