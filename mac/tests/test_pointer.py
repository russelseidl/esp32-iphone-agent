import io

from PIL import Image, ImageDraw

from pointer import Frame, changed_fraction, locate


def frame(w=240, h=520, pointer=None, bg=(40, 90, 160), extra=None) -> Frame:
    """A synthetic mirror frame: flat background, optional grey pointer disc, optional extra rectangle."""
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    if pointer:
        x, y = pointer
        d.ellipse((x - 7, y - 7, x + 7, y + 7), fill=(120, 120, 120))
    if extra:
        d.rectangle(extra, fill=(250, 250, 250))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return Frame.from_jpeg(buf.getvalue(), scale=1)


def test_identical_frames_are_settled():
    a, b = frame(pointer=(100, 200)), frame(pointer=(100, 200))
    assert changed_fraction(a, b) < 0.002


def test_moved_pointer_is_not_settled_but_small():
    a, b = frame(pointer=(100, 200)), frame(pointer=(120, 200))
    assert 0.0005 < changed_fraction(a, b) < 0.01


def test_locate_returns_old_and_new_for_a_right_nudge():
    a, b = frame(pointer=(100, 200)), frame(pointer=(124, 200))
    found = locate(a, b, nudge_sign=+1)
    assert found is not None
    (ox, oy), (nx, ny) = found
    assert abs(ox - 100 / 240) < 0.02 and abs(oy - 200 / 520) < 0.02
    assert abs(nx - 124 / 240) < 0.02 and abs(ny - 200 / 520) < 0.02


def test_locate_handles_a_left_nudge():
    a, b = frame(pointer=(200, 300)), frame(pointer=(176, 300))
    (ox, oy), (nx, ny) = locate(a, b, nudge_sign=-1)
    assert abs(ox - 200 / 240) < 0.02 and abs(nx - 176 / 240) < 0.02


def test_locate_ignores_a_small_distractor_like_a_clock_hand():
    a = frame(pointer=(100, 200), extra=(180, 400, 184, 406))
    b = frame(pointer=(124, 200), extra=(186, 400, 190, 406))  # the hand moved too
    (ox, oy), (nx, ny) = locate(a, b, nudge_sign=+1)
    assert abs(ox - 100 / 240) < 0.02 and abs(nx - 124 / 240) < 0.02


def test_locate_rejects_a_screen_that_changed_elsewhere():
    a = frame(pointer=(100, 200))
    b = frame(pointer=(124, 200), extra=(0, 400, 240, 520))  # a big UI change
    assert locate(a, b, nudge_sign=+1) is None


def test_locate_rejects_no_change():
    a, b = frame(pointer=(100, 200)), frame(pointer=(100, 200))
    assert locate(a, b, nudge_sign=+1) is None


def test_scale_keeps_fractions():
    a = Frame.from_jpeg(frame(pointer=(100, 200)).jpeg, scale=2)
    b = Frame.from_jpeg(frame(pointer=(124, 200)).jpeg, scale=2)
    (ox, oy), _ = locate(a, b, nudge_sign=+1)
    assert abs(ox - 100 / 240) < 0.03 and abs(oy - 200 / 520) < 0.03
