import io

from PIL import Image, ImageDraw

from pointer import Frame
from screen import keyboard_visible


def make(draw_fn, w=360, h=780, bg=(20, 20, 24)) -> Frame:
    im = Image.new("RGB", (w, h), bg)
    draw_fn(ImageDraw.Draw(im), w, h)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return Frame.from_jpeg(buf.getvalue(), scale=1)


def keyboard(d, w, h):
    # four rows of ten keys in the bottom band, as the iOS keyboard draws them
    top = int(h * 0.69)
    for row in range(4):
        y = top + row * int(h * 0.062)
        for k in range(10):
            x = int(w * (0.02 + k * 0.096))
            d.rounded_rectangle((x, y, x + int(w * 0.08), y + int(h * 0.05)), radius=4, fill=(80, 80, 86))


def home_icons(d, w, h):
    for row in range(5):
        for col in range(4):
            x, y = int(w * (0.08 + col * 0.23)), int(h * (0.12 + row * 0.115))
            d.rounded_rectangle((x, y, x + int(w * 0.16), y + int(w * 0.16)), radius=12, fill=(200, 60, 60))


def test_keyboard_rows_are_detected():
    assert keyboard_visible(make(keyboard)) is True


def test_home_screen_is_not_a_keyboard():
    assert keyboard_visible(make(home_icons)) is False


def test_blank_screen_is_not_a_keyboard():
    assert keyboard_visible(make(lambda d, w, h: None)) is False


def test_light_theme_keyboard():
    def light(d, w, h):
        d.rectangle((0, int(h * 0.65), w, h), fill=(210, 212, 218))
        top = int(h * 0.69)
        for row in range(4):
            y = top + row * int(h * 0.062)
            for k in range(10):
                x = int(w * (0.02 + k * 0.096))
                d.rounded_rectangle((x, y, x + int(w * 0.08), y + int(h * 0.05)), radius=4, fill=(255, 255, 255))
    assert keyboard_visible(make(light, bg=(245, 245, 247))) is True
