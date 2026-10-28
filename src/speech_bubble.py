"""Readable, non-modal messages anchored near a desktop companion."""
import math

from PyQt5.QtCore import QPoint, QRect, Qt, QTimer
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)


STYLE = '''
QFrame#bubble { background: #fffdfd; border: 1px solid #ded4ef; border-radius: 16px; }
QLabel#message { color: #443a54; font-family: "Noto Sans CJK SC", sans-serif; font-size: 14px; line-height: 1.4; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QPushButton#close { color: #91809e; background: transparent; border: none; border-radius: 10px; font-size: 16px; }
QPushButton#close:hover { background: #f1eaf7; color: #5d4d70; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 2px; }
QScrollBar::handle:vertical { background: #cfbfdf; border-radius: 2px; min-height: 20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
'''


class SpeechBubble(QWidget):
    """Show full text near a pet; long text scrolls within the screen.

    ``show_message`` accepts the exact text and a visible pet widget. Position is
    best effort on Wayland, where the compositor controls top-level placement.
    """
    WIDTH = 320
    READING_CHARS_PER_SECOND = 4
    MIN_DURATION_MS = 6500
    MAX_DURATION_MS = 60000

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle('Codex Pet 提醒')
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName('bubble')
        layout.addWidget(card)
        row = QHBoxLayout(card)
        row.setContentsMargins(15, 12, 10, 12)
        row.setSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.message = QLabel()
        self.message.setObjectName('message')
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.message.setTextFormat(Qt.PlainText)
        self.scroll.setWidget(self.message)
        row.addWidget(self.scroll, 1)
        self.dismiss = QPushButton('×')
        self.dismiss.setObjectName('close')
        self.dismiss.setToolTip('关闭提醒')
        self.dismiss.setFixedSize(22, 22)
        self.dismiss.clicked.connect(self.hide)
        row.addWidget(self.dismiss, 0, Qt.AlignTop)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def show_message(self, text, anchor, duration_ms=None):
        """Display text, restarting its reading timer; returns duration in ms."""
        self.message.setText(text)
        screen = QApplication.screenAt(anchor.frameGeometry().center()) or anchor.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        width = min(self.WIDTH, max(120, area.width()))
        self.setFixedWidth(width)
        # Account for card margins, close button and a possible scrollbar.
        text_width = max(40, width - 67)
        content_height = self.message.fontMetrics().boundingRect(
            0, 0, text_width, 100000, Qt.TextWordWrap, text).height()
        viewport_height = max(32, min(content_height + 4, max(32, area.height() - 36)))
        self.message.setMinimumHeight(content_height + 4)
        self.scroll.setFixedHeight(viewport_height)
        self.setFixedHeight(min(area.height(), viewport_height + 24))
        self.scroll.verticalScrollBar().setValue(0)
        # X11 supports global placement; a Wayland compositor may choose it.
        rect = anchor.frameGeometry()
        x = rect.center().x() - self.width() // 2
        x = max(area.left(), min(x, area.right() - self.width() + 1))
        above = rect.top() - self.height() - 10
        y = above if above >= area.top() else rect.bottom() + 10
        y = max(area.top(), min(y, area.bottom() - self.height() + 1))
        # The companions can speak in quick succession. Prefer a nearby
        # position that leaves all currently visible speech readable.
        visible = [item for item in QApplication.topLevelWidgets()
                   if isinstance(item, SpeechBubble) and item is not self
                   and item.isVisible() and item.frameGeometry().intersects(area)]
        obstacles = [item.frameGeometry() for item in visible]
        xs = {x, area.left(), area.right() - self.width() + 1}
        ys = {y, area.top(), area.bottom() - self.height() + 1}
        for obstacle in obstacles:
            xs.update((obstacle.left() - self.width() - 4, obstacle.right() + 5))
            ys.update((obstacle.top() - self.height() - 4, obstacle.bottom() + 5))
        xs = {max(area.left(), min(v, area.right() - self.width() + 1)) for v in xs}
        ys = {max(area.top(), min(v, area.bottom() - self.height() + 1)) for v in ys}

        def score(point):
            candidate = QRect(point, self.size())
            overlap = sum(r.width() * r.height() for r in
                          (candidate.intersected(obstacle) for obstacle in obstacles))
            return overlap, abs(point.x() - x) + abs(point.y() - y)

        position = min((QPoint(px, py) for px in xs for py in ys), key=score)
        # If the screen physically cannot hold both bubbles, keep the newest
        # message legible instead of covering the text of either message.
        for item in visible:
            if QRect(position, self.size()).intersects(item.frameGeometry()):
                item.hide()
        self.move(position)
        if duration_ms is None:
            duration_ms = max(self.MIN_DURATION_MS, min(self.MAX_DURATION_MS,
                              math.ceil(len(text) / self.READING_CHARS_PER_SECOND * 1000)))
        self.show()
        self.raise_()
        self.timer.start(max(1, duration_ms))
        return duration_ms

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)
