import pytest

from calibration import Calibration


def test_affine_roundtrip_from_legacy_dict():
    c = Calibration.from_dict({"ax": 0.00372, "bx": 0.04571, "ay": 0.00162, "by": 0.02429})
    assert c.model == "affine"
    assert c.units("x", 0.609) == 151 and c.units("y", 0.921) == 554
    assert abs(c.frac("x", 151) - 0.6075) < 0.002


def test_grid_fit_builds_a_monotone_piecewise_map():
    c = Calibration()
    # x: homed at 0.045, then 60->0.27, 130->0.52, 220->0.82 (sub-linear near the end, as on the real phone)
    c.fit_grid("x", [(0, 0.045), (60, 0.27), (130, 0.52), (220, 0.82)], res=(718, 1560))
    assert c.model == "pwl" and c.res == (718, 1560)
    assert c.units("x", 0.045) == 0 and c.units("x", 0.52) == 130
    assert c.units("x", 0.395) == 95            # midway in the 60..130 segment
    assert c.units("x", 0.67) == 175            # midway in the 130..220 segment
    assert c.units("x", 0.95) == 259            # extrapolated past the last knot on its slope
    assert c.units("x", 0.0) == 0                # never negative


def test_fit_grid_rejects_non_monotone_points():
    c = Calibration()
    with pytest.raises(ValueError):
        c.fit_grid("x", [(0, 0.05), (60, 0.5), (130, 0.4)], res=(718, 1560))


def test_learn_moves_the_nearest_knot_toward_measured_landings():
    c = Calibration()
    c.fit_grid("x", [(0, 0.045), (60, 0.27), (130, 0.52), (220, 0.82)], res=(718, 1560))
    for _ in range(6):
        c.learn("x", 130, 0.50)  # taps aimed at knot 130 keep landing 2% short
    assert abs(c.frac("x", 130) - 0.51) < 0.012  # knot blends toward the measured value, not all the way at once


def test_to_dict_from_dict_roundtrip_and_invalidation_on_resolution_change():
    c = Calibration()
    c.fit_grid("x", [(0, 0.045), (60, 0.27), (130, 0.52), (220, 0.82)], res=(718, 1560))
    c.fit_grid("y", [(0, 0.02), (100, 0.19), (300, 0.5), (520, 0.84)], res=(718, 1560))
    d = Calibration.from_dict(c.to_dict())
    assert d.units("y", 0.5) == 300 and d.valid_for((718, 1560)) and not d.valid_for((480, 1040))


def test_needs_both_axes_before_use():
    c = Calibration()
    c.fit_grid("x", [(0, 0.045), (60, 0.27), (130, 0.52)], res=(718, 1560))
    assert not c.ready
