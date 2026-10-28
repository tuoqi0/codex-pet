"""Pose-independent wing layers for the seated dragon.

The source illustration is flattened, so the exposed wings are separated into
registered image layers with a small painted underlay behind the foreground.
The tail remains in its original pose.
"""
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from PyQt5.QtGui import QImage, QPixmap


# A common canvas avoids one-pixel layout changes between the source poses,
# which differ slightly in size. The added margin belongs to every pose/frame.
_ART_SIZE = (420, 237)
_MARGIN_X = 21
_MARGIN_Y = 12
_COLUMNS = 48
_ROWS = 32
_FRAME_COUNT = 24


def _smoothstep(low, high, value):
    value = max(0.0, min(1.0, (value - low) / (high - low)))
    return value * value * (3.0 - 2.0 * value)


def _prepare_art(path):
    if isinstance(path, Image.Image):
        original = path.convert('RGBA')
    else:
        with Image.open(path) as source:
            original = source.convert('RGBA')
    original.thumbnail(_ART_SIZE, Image.Resampling.LANCZOS)
    width, height = _ART_SIZE
    canvas = Image.new('RGBA', (width + 2 * _MARGIN_X, height + 2 * _MARGIN_Y))
    canvas.paste(original, (_MARGIN_X + (width - original.width) // 2,
                            _MARGIN_Y + (height - original.height) // 2))
    return canvas


@lru_cache(maxsize=1)
def _wing_assets():
    folder = Path(__file__).resolve().parent
    if not (folder / 'wing-cutout.png').exists():
        folder = folder.parent / 'assets'
    return tuple(Image.open(folder / name).copy() for name in
                 ('wing-cutout.png', 'wing-underlay.png', 'wing-left.png', 'wing-right.png'))


def _wing_displacement(index, side):
    """Vertices depend on the animation phase, not the selected pose."""
    phase = 2 * math.pi * index / _FRAME_COUNT
    t = phase if side == 0 else phase - .45
    x, y = (785, 350) if side == 0 else (1210, 380)
    cx = _MARGIN_X + _ART_SIZE[0] * x / 1671
    cy = _MARGIN_Y + _ART_SIZE[1] * y / 941
    amplitude = 5 if side == 0 else 10
    width, height = _ART_SIZE[0] + 2 * _MARGIN_X, _ART_SIZE[1] + 2 * _MARGIN_Y
    xs = [round(width * n / _COLUMNS) for n in range(_COLUMNS + 1)]
    ys = [round(height * n / _ROWS) for n in range(_ROWS + 1)]
    def at(x, y):
        distance = math.hypot(x - cx, y - cy)
        weight = _smoothstep(5, 35, distance)
        y += amplitude * math.sin(t) * weight
        angle = math.radians((5 if side == 0 else -5) * math.sin(t))
        cosine, sine = math.cos(angle), math.sin(angle)
        dx, dy = x - cx, y - cy
        return (cx + cosine * dx - sine * dy, cy + sine * dx + cosine * dy)
    points = [[at(x, y) for y in ys] for x in xs]
    return [((x0, y0, x1, y1), (*points[xi][yi], *points[xi][yi + 1], *points[xi + 1][yi + 1], *points[xi + 1][yi]))
            for xi, (x0, x1) in enumerate(zip(xs, xs[1:]))
            for yi, (y0, y1) in enumerate(zip(ys, ys[1:]))]


@lru_cache(maxsize=_FRAME_COUNT)
def _wing_background(index):
    """Cache pose-independent wing layers after compositing once per phase."""
    _, underlay, left, right = _wing_assets()
    composed = underlay.copy()
    foreground_guard = Image.new('L', underlay.size, 255)
    ImageDraw.Draw(foreground_guard).rectangle((145, 115, 205, 175), fill=0)
    for wing, side in ((left, 0), (right, 1)):
        wing = wing.transform(underlay.size, Image.Transform.MESH, _wing_displacement(index, side), Image.Resampling.BICUBIC)
        wing.putalpha(ImageChops.multiply(wing.getchannel('A'), foreground_guard))
        composed.alpha_composite(wing)
    return composed


def motion_frames(path):
    """Return twenty-four QPixmaps in one consistent, transparent padded canvas.

    Accepts an image path or an already loaded PIL Image, matching the old API.
    A QApplication must exist before constructing QPixmaps.
    """
    original = _prepare_art(path)
    width, height = original.size
    total_mask, _, _, _ = _wing_assets()
    alpha = original.getchannel('A')
    body = original.copy()
    body.putalpha(ImageChops.multiply(alpha, ImageChops.invert(total_mask)))
    frames = []
    for index in range(_FRAME_COUNT):
        composed = _wing_background(index).copy()
        composed.alpha_composite(body)
        image = QImage(composed.tobytes(), width, height, width * 4, QImage.Format_RGBA8888).copy()
        frames.append(QPixmap.fromImage(image))
    return frames
