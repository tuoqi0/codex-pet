#!/usr/bin/python3
"""Start the real desktop client and display a short, dismissible opening."""
import argparse
import math
import subprocess
import sys
import time
from pathlib import Path


def ease(value):
    value = max(0.0, min(1.0, value))
    return 1.0 - (1.0 - value) ** 3


# The 32 frames form a 3.2-second animation: left to right, then top to bottom.
SPRITE_COLUMNS = 8
SPRITE_ROWS = 4
SPRITE_WIDTH = 462
SPRITE_HEIGHT = 261
SPRITE_FRAME_SECONDS = .1


def asset_path(name):
    root = Path(__file__).resolve().parent.parent
    packaged = root / name
    return packaged if packaged.is_file() else root / 'assets' / name


def sprite_geometry(width, height):
    if width <= 0 or height <= 0 or width % SPRITE_COLUMNS or height % SPRITE_ROWS:
        raise ValueError('无法加载开场动画素材')
    frame_width, frame_height = width // SPRITE_COLUMNS, height // SPRITE_ROWS
    if frame_width * SPRITE_HEIGHT != frame_height * SPRITE_WIDTH:
        raise ValueError('无法加载开场动画素材')
    return frame_width, frame_height


def sprite_frame(time_elapsed):
    return min(int(max(0.0, time_elapsed) / SPRITE_FRAME_SECONDS), SPRITE_COLUMNS * SPRITE_ROWS - 1)


def run_opening():
    from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF
    from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QRadialGradient, QLinearGradient
    from PyQt5.QtWidgets import QApplication, QWidget

    class Opening(QWidget):
        def __init__(self, image):
            super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            self.setFixedSize(620, 360)
            geometry = QApplication.primaryScreen().availableGeometry()
            self.move(geometry.center().x() - self.width() // 2, geometry.center().y() - self.height() // 2)
            self.image = QPixmap(str(image))
            self.frame_width, self.frame_height = sprite_geometry(self.image.width(), self.image.height())
            self.start = time.monotonic()
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.advance)
            self.timer.start(33)

        def advance(self):
            age = time.monotonic() - self.start
            if age >= 3.65:
                self.close()
                return
            if age > 3.1:
                self.setWindowOpacity(max(0.0, (3.65 - age) / .55))
            self.update()

        def closeEvent(self, event):
            self.timer.stop()
            event.accept()
            QApplication.quit()

        def mousePressEvent(self, event):
            self.close()

        def keyPressEvent(self, event):
            if event.key() == Qt.Key_Escape:
                self.close()
            else:
                super().keyPressEvent(event)

        def paintEvent(self, event):
            t = time.monotonic() - self.start
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            glow = QRadialGradient(310, 165, 390)
            glow.setColorAt(0, QColor('#8b79c9'))
            glow.setColorAt(.29, QColor('#483c72'))
            glow.setColorAt(.66, QColor('#201a38'))
            glow.setColorAt(1, QColor('#101020'))
            painter.fillRect(self.rect(), glow)
            painter.save()
            painter.translate(310, 164)
            painter.rotate(-20 * (1 - ease(t / 2.5)))
            ring = ease(t / 1.0)
            painter.scale(.65 + .35 * ring, .65 + .35 * ring)
            for radius, alpha, style in [(152, 145, Qt.SolidLine), (138, 95, Qt.DashLine), (119, 50, Qt.SolidLine)]:
                painter.setPen(QPen(QColor(231, 215, 255, int(alpha * ring)), 1, style))
                painter.drawEllipse(QPointF(0, 0), radius, radius)
            painter.restore()
            for i in range(23):
                x = 35 + ((i * 137 + 41) % 550)
                y = 35 + ((i * 97 + 29) % 245) - 13 * min(t, 3)
                a = int(150 * ease((t - (i % 6) * .12) / .55) * (.5 + .5 * math.sin(t * 3 + i)))
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(230, 216, 255, max(0, a)))
                painter.drawEllipse(QPointF(x, y), 1.1 + i % 3 * .45, 1.1 + i % 3 * .45)
            painter.save()
            figure = ease((t - .12) / 1.3)
            painter.setOpacity(figure)
            zoom = .84 + .16 * ease(t / 2.5)
            w, h = 590 * zoom, (590 * SPRITE_HEIGHT / SPRITE_WIDTH) * zoom
            frame = sprite_frame(t)
            source = QRectF((frame % SPRITE_COLUMNS) * self.frame_width,
                            (frame // SPRITE_COLUMNS) * self.frame_height,
                            self.frame_width, self.frame_height)
            painter.drawPixmap(QRectF((620 - w) / 2, (320 - h) / 2 + 7 * (1 - figure), w, h), self.image, source)
            painter.restore()
            # A short travelling light sweep, fading out before it crosses the title.
            if .3 < t < 2.4:
                sweep = (t - .3) / 2.1
                beam = QLinearGradient(0, 0, 100, 0)
                beam.setColorAt(0, QColor(220, 200, 255, 0))
                beam.setColorAt(.5, QColor(240, 225, 255, int(70 * math.sin(math.pi * sweep))))
                beam.setColorAt(1, QColor(220, 200, 255, 0))
                painter.save()
                painter.translate(-100 + sweep * 820, 0)
                painter.rotate(18)
                painter.fillRect(QRectF(-50, -100, 100, 560), beam)
                painter.restore()
            painter.setOpacity(ease((t - .7) / 1.1))
            painter.setPen(QColor('#f2ebff'))
            font = QFont('Sans', 10)
            font.setLetterSpacing(QFont.AbsoluteSpacing, 3.2)
            painter.setFont(font)
            painter.drawText(QRectF(0, 321, 620, 26), Qt.AlignCenter, 'CODEX · COMPANION')

    app = QApplication(sys.argv[:1])
    opening = Opening(asset_path('opening-sprite.png'))
    opening.show()
    return app.exec_()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--command', required=True)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
        subprocess.Popen([args.command, *arguments], start_new_session=True)
    except OSError as exc:
        print(f'启动桌面客户端失败：{exc}', file=sys.stderr)
        return 1
    try:
        return run_opening()
    except (ImportError, ValueError) as exc:
        print(f'无法显示开场：{exc}', file=sys.stderr)
        return 0  # The real client has already been started.


if __name__ == '__main__':
    raise SystemExit(main())
