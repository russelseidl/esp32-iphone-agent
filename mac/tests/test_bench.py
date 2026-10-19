import io

from PIL import Image, ImageDraw

from bench import Trial, find_dots, match_dots, pick_winners, summarize, to_markdown
from pointer import Frame


def canvas(dots, w=360, h=780) -> Frame:
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for x, y in dots:  # fractions
        cx, cy = x * w, y * h
        d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=(30, 30, 30))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return Frame.from_jpeg(buf.getvalue(), scale=1)


def test_find_dots_returns_centres_in_units():
    f = canvas([(0.25, 0.3), (0.75, 0.6)])
    dots = find_dots(f)
    assert len(dots) == 2
    assert min(abs(d[0] - 250) + abs(d[1] - 300) for d in dots) < 12


def test_find_dots_ignores_a_non_white_screen():
    assert find_dots(canvas([])) == []
    im = Image.new("RGB", (360, 780), (40, 40, 40))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    assert find_dots(Frame.from_jpeg(buf.getvalue(), scale=1)) is None  # not a canvas


def test_match_dots_pairs_nearest_and_reports_misses():
    planned = [(250, 300), (750, 600), (500, 900)]
    found = [(258, 296), (740, 612)]
    errors = match_dots(planned, found)
    assert errors[0] == round((8 ** 2 + 4 ** 2) ** 0.5, 1) and errors[1] == round((10 ** 2 + 12 ** 2) ** 0.5, 1)
    assert errors[2] is None  # no dot for the third tap


def test_summarize_and_pick_winners():
    trials = [Trial("res", "480x1040", error=12, latency_ms=2100, drops=0),
              Trial("res", "480x1040", error=10, latency_ms=1900, drops=0),
              Trial("res", "360x780", error=14, latency_ms=1500, drops=0),
              Trial("res", "360x780", error=13, latency_ms=1400, drops=1),
              Trial("fps", "5", error=11, latency_ms=2000, drops=0),
              Trial("fps", "8", error=None, latency_ms=2600, drops=0)]
    s = summarize(trials)
    assert s["res"]["480x1040"]["error_mean"] == 11 and s["res"]["360x780"]["latency_p50"] == 1450
    assert s["fps"]["8"]["error_mean"] is None and s["fps"]["8"]["misses"] == 1
    w = pick_winners(s)
    assert w["res"]["winner"] == "360x780" and w["res"]["by"] == "latency"
    assert w["fps"]["winner"] == "5" and w["fps"]["by"] == "baseline"
    md = to_markdown(s, w)
    assert "| res |" in md and "360x780" in md
