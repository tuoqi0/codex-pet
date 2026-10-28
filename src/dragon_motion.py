"""Local motion for the seated dragon illustration.

The supplied artwork is a single flattened image, not a character rig.  This
inverse mesh displaces only regions near the wings and tail; it cannot reveal
parts hidden behind them.  The small, smoothly varying field avoids moving the
face and knees and leaves a transparent margin for the tail's motion.
"""
import math

from PIL import Image
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


def _band(start, rise, fall, end, value):
    return _smoothstep(start, rise, value) * (1.0 - _smoothstep(fall, end, value))


def _source_point(x, y, phase):
    """Map an output point to its source; units x/y are padded-canvas pixels."""
    width, height = _ART_SIZE
    nx, ny = (x - _MARGIN_X) / width, (y - _MARGIN_Y) / height
    left = _band(.10, .18, .30, .39, ny) * (1.0 - _smoothstep(.36, .49, nx))
    # Broad ramps let the right wing and tail travel farther without folding
    # the mesh; the left wing remains limited by the adjacent knee.
    right = _band(-.10, .23, .35, .56, ny) * _smoothstep(.69, .82, nx)
    tail = _band(.38, .68, 1.00, 1.30, ny) * _smoothstep(.68, .86, nx)
    wing_phase = math.sin(phase)
    tail_phase = math.sin(phase + .8)
    # Gentle amplitudes and broad ramps keep each mesh cell oriented: stronger
    # fields fold the flattened wing membrane and reveal transparent holes.
    return (x + width * .045 * tail * tail_phase,
            y + height * (.04 * left + .075 * right) * wing_phase
            + height * .045 * tail * tail_phase)


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


def motion_frames(path):
    """Return twenty-four QPixmaps in one consistent, transparent padded canvas.

    Accepts an image path or an already loaded PIL Image, matching the old API.
    A QApplication must exist before constructing QPixmaps.
    """
    original = _prepare_art(path)
    width, height = original.size
    xs = [round(width * n / _COLUMNS) for n in range(_COLUMNS + 1)]
    ys = [round(height * n / _ROWS) for n in range(_ROWS + 1)]
    frames = []
    for index in range(_FRAME_COUNT):
        phase = 2 * math.pi * index / _FRAME_COUNT
        mesh = []
        for x0, x1 in zip(xs, xs[1:]):
            for y0, y1 in zip(ys, ys[1:]):
                quad = (*_source_point(x0, y0, phase), *_source_point(x0, y1, phase),
                        *_source_point(x1, y1, phase), *_source_point(x1, y0, phase))
                mesh.append(((x0, y0, x1, y1), quad))
        warped = original.transform(original.size, Image.Transform.MESH, mesh,
                                    resample=Image.Resampling.BICUBIC)
        image = QImage(warped.tobytes(), width, height, width * 4, QImage.Format_RGBA8888).copy()
        frames.append(QPixmap.fromImage(image))
    return frames
