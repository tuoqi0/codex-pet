"""A small, shared quick panel for the desktop companions.

The caller owns all network activity and passes App Server results to the UI.
No quota is estimated from an unrelated account or cached as a fresh result.
"""
import math
from datetime import datetime

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


STYLE = """
QFrame#floatingCard { background: #fffdfd; border: 1px solid #e4def2; border-radius: 18px; }
QScrollArea#floatingScroll { background: transparent; border: 0; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 5px 2px; }
QScrollBar::handle:vertical { background: #cbb9de; border-radius: 2px; min-height: 22px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QWidget { font-family: "Noto Sans CJK SC", "Noto Sans", sans-serif; color: #403951; font-size: 12px; }
QLabel#title { font-size: 17px; font-weight: 700; color: #382f50; }
QLabel#subtitle { font-size: 11px; color: #888099; }
QLabel#section { font-size: 12px; font-weight: 700; color: #57486d; }
QLabel#metric { font-size: 11px; font-weight: 600; color: #5b506d; }
QLabel#amount { font-size: 11px; font-weight: 700; color: #7656aa; }
QLabel#reset, QLabel#state { font-size: 11px; color: #8c849b; }
QFrame#usageCard { background: #f8f5fd; border: 1px solid #eee9f7; border-radius: 12px; }
QProgressBar { background: #e9e2f3; border: 0; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #ad91db; border-radius: 3px; }
QPushButton { background: #f5f1fa; color: #5e5075; border: 1px solid #e9e0f3; border-radius: 9px; padding: 8px 4px; font-weight: 600; }
QPushButton:hover { background: #ece3f7; border-color: #d2c0e8; }
QPushButton:pressed { background: #e4d7f1; }
QPushButton#chat { background: #a98bcf; border-color: #a98bcf; color: white; }
QPushButton#chat:hover { background: #9978c4; }
QPushButton#small { border: 0; background: transparent; color: #8a7c9a; padding: 2px 5px; }
QPushButton#small:hover { background: #f0ebf7; }
"""


def _duration(minutes):
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or not math.isfinite(minutes) or minutes <= 0:
        return '窗口不可用'
    if minutes % 1440 == 0:
        return f'{minutes / 1440:g} 天窗口'
    if minutes % 60 == 0:
        return f'{minutes / 60:g} 小时窗口'
    return f'{minutes:g} 分钟窗口'


def _reset(timestamp):
    try:
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            return '重置时间不可用'
        return '重置 ' + datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OSError, OverflowError):
        return '重置时间不可用'


class _QuotaRow(QWidget):
    def __init__(self, label):
        super().__init__()
        self.label = label
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        header = QHBoxLayout()
        self.caption = QLabel(label)
        self.caption.setObjectName('metric')
        self.caption.setWordWrap(True)
        self.amount = QLabel('不可用')
        self.amount.setObjectName('amount')
        header.addWidget(self.caption, 1)
        header.addWidget(self.amount, 0, Qt.AlignRight | Qt.AlignTop)
        layout.addLayout(header)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        self.bar.setFixedHeight(6)
        layout.addWidget(self.bar)
        self.reset = QLabel('重置时间不可用')
        self.reset.setObjectName('reset')
        self.reset.setWordWrap(True)
        layout.addWidget(self.reset)
        self.set_window(None)

    def set_window(self, window):
        window = window if isinstance(window, dict) else {}
        self.caption.setText(f'{self.label} · {_duration(window.get("windowDurationMins"))}')
        used = window.get('usedPercent')
        valid = isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) and 0 <= used <= 100
        self.amount.setText(f'已用 {used:g}%' if valid else '不可用')
        self.bar.setVisible(valid)
        if valid:
            self.bar.setValue(round(used))
        self.reset.setText(_reset(window.get('resetsAt')))


class PetFloating(QWidget):
    """Floating pet controls; weather and quota refresh have separate callbacks."""

    def __init__(self, on_action=None, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.on_action = on_action or (lambda action: None)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle('Codex Pet 快捷面板')
        self.setStyleSheet(STYLE)
        self.setFixedWidth(320)
        self._screen = None
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_to_screen)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setObjectName('floatingScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(self.scroll)
        card = QFrame()
        card.setObjectName('floatingCard')
        self.scroll.setWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(17, 15, 17, 15)
        layout.setSpacing(11)

        heading = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel('✦  嗨，我在这里')
        title.setObjectName('title')
        subtitle = QLabel('想聊聊，还是一起完成什么？')
        subtitle.setObjectName('subtitle')
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        heading.addLayout(titles, 1)
        close = QPushButton('✕')
        close.setObjectName('small')
        close.setToolTip('关闭快捷面板')
        close.clicked.connect(self.hide)
        heading.addWidget(close, 0, Qt.AlignTop)
        layout.addLayout(heading)

        usage = QFrame()
        usage.setObjectName('usageCard')
        usage_layout = QVBoxLayout(usage)
        usage_layout.setContentsMargins(12, 11, 12, 11)
        usage_layout.setSpacing(9)
        usage_header = QHBoxLayout()
        usage_title = QLabel('Codex 额度')
        usage_title.setObjectName('section')
        usage_header.addWidget(usage_title)
        usage_header.addStretch()
        refresh = QPushButton('刷新 ↻')
        refresh.setObjectName('small')
        refresh.clicked.connect(lambda: self.on_action('refresh'))
        usage_header.addWidget(refresh)
        usage_layout.addLayout(usage_header)
        self.state = QLabel('尚未查询')
        self.state.setObjectName('state')
        self.state.setWordWrap(True)
        self.state.setTextInteractionFlags(Qt.TextSelectableByMouse)
        usage_layout.addWidget(self.state)
        self.primary = _QuotaRow('主要额度')
        self.secondary = _QuotaRow('次要额度')
        usage_layout.addWidget(self.primary)
        usage_layout.addWidget(self.secondary)
        layout.addWidget(usage)

        weather_header = QHBoxLayout()
        weather_title = QLabel('天气')
        weather_title.setObjectName('section')
        weather_header.addWidget(weather_title)
        weather_header.addStretch()
        weather_refresh = QPushButton('刷新 ↻')
        weather_refresh.setObjectName('small')
        weather_refresh.setToolTip('立即查询已选地点的天气')
        weather_refresh.clicked.connect(lambda: self.on_action('weather'))
        weather_header.addWidget(weather_refresh)
        layout.addLayout(weather_header)
        self.weather = QLabel('天气：未设置城市')
        self.weather.setObjectName('subtitle')
        self.weather.setWordWrap(True)
        self.weather.setToolTip('在陪伴设置中选择城市后才会查询天气。')
        layout.addWidget(self.weather)
        self.weather_detail = QLabel('')
        self.weather_detail.setObjectName('state')
        self.weather_detail.setWordWrap(True)
        self.weather_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.weather_detail.hide()
        layout.addWidget(self.weather_detail)

        buttons = QGridLayout()
        buttons.setSpacing(7)
        self.buttons = {}
        for i, (action, text) in enumerate((('chat', '✧  聊天'), ('plan', '◌  计划'),
                                              ('task', '↗  任务'), ('settings', '♡  陪伴设置'))):
            button = QPushButton(text)
            if action == 'chat':
                button.setObjectName('chat')
            button.clicked.connect(lambda checked=False, action=action: self.on_action(action))
            buttons.addWidget(button, i // 2, i % 2)
            self.buttons[action] = button
        self.buttons['refresh'] = refresh
        self.buttons['weather'] = weather_refresh
        layout.addLayout(buttons)

    def set_loading(self):
        self.state.setText('正在查询 Codex 额度…')
        self.state.setToolTip('')
        self.primary.set_window(None)
        self.secondary.set_window(None)
        self._content_changed()

    def set_weather(self, text, detail=''):
        self.weather.setText(text)
        self.weather.setToolTip(detail or text)
        self.weather_detail.setText(detail)
        self.weather_detail.setVisible(bool(detail))
        self._content_changed()

    def set_error(self, message):
        text = '查询失败：' + str(message)
        self.state.setText(text if len(text) <= 160 else text[:157] + '…')
        self.state.setToolTip(text)
        self.primary.set_window(None)
        self.secondary.set_window(None)
        self._content_changed()

    def set_limits(self, data):
        data = data if isinstance(data, dict) else {}
        limit = data.get('rateLimits')
        if not isinstance(limit, dict) or not limit:
            entries = data.get('rateLimitsByLimitId')
            limit = entries.get('codex') if isinstance(entries, dict) else None
        limit = limit if isinstance(limit, dict) else {}
        self.primary.set_window(limit.get('primary'))
        self.secondary.set_window(limit.get('secondary'))
        self.state.setText('仅显示 Codex 返回的用量' if limit else '额度不可用')
        self.state.setToolTip('')
        self._content_changed()

    def _content_changed(self):
        self.adjustSize()
        if self.isVisible() and QApplication.platformName().lower() == 'xcb':
            # Qt finishes text wrapping and layout activation on the next event
            # loop iteration. Recheck then, not only before the new size lands.
            self._fit_timer.start(0)

    def _fit_to_screen(self):
        if not self.isVisible() or QApplication.platformName().lower() != 'xcb':
            return
        screen = self._screen or QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if not screen:
            return
        area = screen.availableGeometry()
        self.setFixedWidth(min(320, area.width()))
        self.setMaximumHeight(area.height())
        card = self.scroll.widget()
        card.layout().activate()
        # QScrollArea.sizeHint() can use the card's preferred width rather
        # than the actual viewport width; that leaves a few pixels of false
        # overflow on real X11. Size from the card at the assigned width.
        content_height = max(card.sizeHint().height(), card.minimumSizeHint().height(),
                             card.heightForWidth(self.width())) + 4
        fits = content_height <= area.height()
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff if fits else Qt.ScrollBarAsNeeded)
        self.resize(self.width(), min(content_height, area.height()))
        frame = self.frameGeometry()
        x = max(area.left(), min(frame.left(), area.right() - frame.width() + 1))
        y = max(area.top(), min(frame.top(), area.bottom() - frame.height() + 1))
        self.move(x, y)

    def open_for(self, pet):
        """Place near the pet on X11; Wayland owns top-level window placement."""
        self.adjustSize()
        platform = QApplication.platformName().lower()
        if platform == 'xcb':
            screen = QApplication.screenAt(pet.frameGeometry().center()) or pet.screen() or QApplication.primaryScreen()
            if screen:
                self._screen = screen
                area = screen.availableGeometry()
                self.setFixedWidth(min(320, area.width()))
                self.setMaximumHeight(area.height())
                self.adjustSize()
                pet_rect = pet.frameGeometry()
                x = pet_rect.right() + 12
                if x + self.width() > area.right() + 1:
                    x = pet_rect.left() - self.width() - 12
                x = max(area.left(), min(x, area.right() - self.width() + 1))
                y = max(area.top(), min(pet_rect.top(), area.bottom() - self.height() + 1))
                self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        if platform == 'xcb':
            self._fit_timer.start(0)
