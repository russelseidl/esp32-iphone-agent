import math

from stroke import Curve, plan_path, score, ink_pixels, dist_to_path

LINEAR = Curve([(2, 4.8), (10, 24.0), (40, 96.0), (127, 304.8)])
ACCEL = Curve([(2, 3.5), (5, 8.0), (10, 24.0), (20, 60.0), (40, 150.0), (80, 400.0), (127, 700.0)])


def simulate(plan, curve, origin):
    """Where the pointer ends up if the curve is exact: the model's own prediction."""
    x, y = origin
    pts = []
    for dx, dy in plan.reports:
        mag = math.hypot(dx, dy)
        g = curve.gain(mag)
        x, y = x + dx * g, y + dy * g
        pts.append((x, y))
    return pts


def test_curve_interpolates_and_inverts():
    assert ACCEL.px(10) == 24.0
    assert ACCEL.px(15) == 42.0           # halfway between 24 and 60
    assert ACCEL.px(1) == 0.0 and ACCEL.px(1.9) == 0.0   # the dead zone
    assert ACCEL.step_for(24.0) == 10 and ACCEL.step_for(42.0) == 15 and ACCEL.step_for(0.2) == 0
    assert Curve.from_dict(ACCEL.to_dict()).points == ACCEL.points


def test_straight_line_lands_on_the_target():
    path = [(100.0, 100.0), (400.0, 250.0)]
    plan = plan_path(path, ACCEL, px_per_report=5.0)
    assert plan.reports and all(abs(dx) <= 127 and abs(dy) <= 127 for dx, dy in plan.reports)
    end = plan.end
    assert math.dist(end, path[-1]) < 1.5
    # every predicted position stays near the line
    assert max(dist_to_path(p, path) for p in plan.predicted) < 1.5


def test_polyline_with_corners_tracks_the_path():
    path = [(50.0, 50.0), (250.0, 50.0), (250.0, 300.0), (50.0, 300.0), (50.0, 50.0)]
    plan = plan_path(path, LINEAR, px_per_report=3.0)
    assert math.dist(plan.end, path[-1]) < 1.5
    # corners can overshoot by at most half the smallest possible report (the dead zone sets it)
    assert max(dist_to_path(p, path) for p in plan.predicted) <= LINEAR.px(2) / 2 + 0.1
    # the prediction and an independent simulation agree (no hidden state in the planner)
    sim = simulate(plan, LINEAR, path[0])
    assert math.dist(sim[-1], plan.end) < 1e-6


def test_error_diffusion_beats_rounding_on_a_shallow_line():
    # a line whose slope cannot be hit by integer steps: diffusion keeps the mean offset near zero
    path = [(0.0, 0.0), (300.0, 20.0)]
    plan = plan_path(path, LINEAR, px_per_report=2.4)
    devs = [dist_to_path(p, path) for p in plan.predicted]
    assert sum(devs) / len(devs) < 0.6 and max(devs) < 1.5


def test_score_measures_deviation_and_coverage():
    w, h = 100, 60
    gray = [255] * (w * h)
    path = [(10.0, 30.0), (90.0, 30.0)]
    for x in range(10, 91):  # ink exactly on the path
        gray[30 * w + x] = 0
    ink = ink_pixels(gray, w, h)
    s = score(ink, path)
    assert s["deviation"] == 0.0 and s["coverage"] == 1.0 and s["stray"] == 0.0 and s["ink"] == 81
    assert s["center_dev"] == 0.0
    for x in range(10, 91):  # a second line 4 px off, half the ink now deviates
        gray[34 * w + x] = 0
    s2 = score(ink_pixels(gray, w, h), path)
    assert 1.9 < s2["deviation"] < 2.1 and s2["coverage"] == 1.0
    assert 1.9 < s2["center_dev"] < 2.1  # the centroid of both lines sits 2 px off the path
    assert score([], path)["deviation"] is None


def test_planner_never_emits_dead_zone_reports():
    path = [(0.0, 0.0), (120.0, 7.0)]
    plan = plan_path(path, ACCEL, px_per_report=3.5)
    assert plan.reports and all(math.hypot(dx, dy) >= 2 for dx, dy in plan.reports)
    assert math.dist(plan.end, path[-1]) < 2.0
