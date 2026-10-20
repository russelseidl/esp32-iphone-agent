from absmap import ABS_MAX, AbsMap, fit, fit_axis


def test_default_map_spans_the_screen():
    m = AbsMap()
    assert not m.measured
    assert m.abs("x", 0.0) == 0 and m.abs("x", 1.0) == ABS_MAX and m.abs("y", 0.5) == round(ABS_MAX / 2)
    assert abs(m.frac("y", ABS_MAX) - 1.0) < 1e-9


def test_fit_recovers_a_line_with_noise():
    xs = [(4915, 0.1502), (16384, 0.5010), (27852, 0.8498)]   # frac = abs / 32767 with +-0.0005 noise
    ys = [(4915, 0.1301), (16384, 0.4802), (27852, 0.8299)]   # offset: the pointer rests 0.13 lower than 0.15
    m = fit(xs, ys)
    assert m.measured and m.residual < 0.001
    assert abs(m.abs("x", 0.5) - 16384) <= 20
    assert abs(m.frac("y", 16384) - 0.4802) < 0.001
    assert m.abs("x", -0.5) == 0 and m.abs("x", 2.0) == ABS_MAX   # clamped to the device range


def test_fit_needs_distinct_points():
    try:
        fit_axis([(100, 0.1), (100, 0.2)])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_save_and_load(tmp_path):
    m = fit([(0, 0.01), (ABS_MAX, 0.99)], [(0, 0.05), (ABS_MAX, 0.95)])
    m.save(tmp_path / "abs.json")
    m2 = AbsMap.load(tmp_path / "abs.json")
    assert m2.to_dict() == m.to_dict()
    assert not AbsMap.load(tmp_path / "missing.json").measured
