"""X11 input shaping; never changes the painted window bounding region."""
import ctypes
import ctypes.util
import math
from PIL import Image, ImageFilter
from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QImage, QPainter
from PyQt5.QtWidgets import QApplication

class _Rect(ctypes.Structure):
    _fields_ = [('x', ctypes.c_short), ('y', ctypes.c_short),
                ('width', ctypes.c_ushort), ('height', ctypes.c_ushort)]

class InputShape:
    def __init__(self):
        self.display = None
        if QApplication.platformName().lower() != 'xcb':
            return
        try:
            self.x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
            self.ext = ctypes.CDLL(ctypes.util.find_library('Xext'))
            self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            self.x11.XOpenDisplay.restype = ctypes.c_void_p
            self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            self.x11.XFlush.argtypes = [ctypes.c_void_p]
            self.ext.XShapeQueryExtension.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            self.ext.XShapeQueryVersion.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            self.ext.XShapeCombineRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.POINTER(_Rect), ctypes.c_int, ctypes.c_int, ctypes.c_int]
            self.display = self.x11.XOpenDisplay(None)
            event, error = ctypes.c_int(), ctypes.c_int()
            if self.display:
                major, minor = ctypes.c_int(), ctypes.c_int()
                supported = (self.ext.XShapeQueryExtension(self.display, ctypes.byref(event), ctypes.byref(error))
                             and self.ext.XShapeQueryVersion(self.display, ctypes.byref(major), ctypes.byref(minor))
                             and (major.value, minor.value) >= (1, 1))
                if not supported:
                    self.close()
        except (OSError, TypeError, AttributeError):
            self.display = None

    def close(self):
        if self.display:
            self.x11.XCloseDisplay(self.display)
            self.display = None

    def set(self, widget, image, margin=4):
        if not self.display or image.isNull():
            return
        image = image.convertToFormat(QImage.Format_RGBA8888)
        ptr = image.constBits()
        ptr.setsize(image.byteCount())
        alpha = Image.frombytes('RGBA', (image.width(), image.height()), bytes(ptr)).getchannel('A')
        # Work on small cells: this keeps frame-by-frame Q animation cheap
        # even for enlarged pets while retaining a generous hit margin.
        cell = 3
        full_w, full_h = alpha.size
        w, h = (full_w + cell - 1) // cell, (full_h + cell - 1) // cell
        alpha = alpha.resize((w, h), Image.Resampling.BOX).point(lambda n: 255 if n else 0)
        radius = max(1, math.ceil(margin * widget.devicePixelRatioF() / cell))
        alpha = alpha.filter(ImageFilter.MaxFilter(radius * 2 + 1))
        data = alpha.tobytes()
        rects = []
        for y in range(h):
            row = data[y*w:(y+1)*w]
            x = 0
            while x < w:
                while x < w and not row[x]: x += 1
                start = x
                while x < w and row[x]: x += 1
                if x > start:
                    rects.append(_Rect(start * cell, y * cell,
                                       min(x * cell, full_w) - start * cell,
                                       min((y + 1) * cell, full_h) - y * cell))
        if not rects:
            return
        array = (_Rect * len(rects))(*rects)
        self.ext.XShapeCombineRectangles(self.display, int(widget.winId()), 2, 0, 0, array, len(rects), 0, 0)
        self.x11.XFlush(self.display)

    def image(self, widget):
        ratio = widget.devicePixelRatioF()
        image = QImage(round(widget.width()*ratio), round(widget.height()*ratio), QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.transparent)
        return image


def dragon_outline(widget):
    if not widget.input_shape.display:
        return
    image = widget.input_shape.image(widget)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    for frames in widget.frames.values():
        # Sample the extrema of the 24-frame deformation so the region keeps
        # covering the moving wings throughout the cycle.
        for frame in frames[::6]:
            bounds = frame.size().scaled(470, 266, Qt.KeepAspectRatio)
            x, y = (widget.WIDTH - bounds.width()) / 2, (widget.HEIGHT - bounds.height()) / 2
            for angle in (-9, 0, 9):
                for dy in (-8, 3):
                    painter.save()
                    painter.scale(widget.scale_factor, widget.scale_factor)
                    painter.translate(widget.WIDTH / 2, widget.HEIGHT / 2 + dy)
                    painter.rotate(angle)
                    painter.translate(-widget.WIDTH / 2, -widget.HEIGHT / 2)
                    painter.drawPixmap(QRectF(x, y, bounds.width(), bounds.height()), frame, QRectF(frame.rect()))
                    painter.restore()
    painter.end()
    widget.input_shape.set(widget, image, margin=10)


def q_outline(widget):
    if not widget.input_shape.display or widget.player.image is None:
        return
    image = widget.input_shape.image(widget)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    source = widget.player.image
    entry = widget.player.entries.get(widget.player.current, {})
    scale = entry.get('renderScale', 1.0)
    offset = entry.get('renderOffset', [0, 0])
    if not isinstance(scale, (int, float)) or not math.isfinite(scale): scale = 1.0
    if not isinstance(offset, list) or len(offset) != 2 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in offset): offset = [0, 0]
    bounds = source.size().scaled(widget.width(), widget.height(), Qt.KeepAspectRatio)
    w, h = bounds.width()*scale, bounds.height()*scale
    rect = QRectF((widget.width()-w)/2 + offset[0]*widget.width(), widget.height()-h + offset[1]*widget.height(), w, h)
    painter.drawImage(rect, source, QRectF(source.rect()))
    painter.end()
    widget.input_shape.set(widget, image, margin=6)
