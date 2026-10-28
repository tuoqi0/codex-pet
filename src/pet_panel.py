"""Shared Codex Pet conversation and usage window."""
import json
import math
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QPainter, QPen, QPixmap, QTextCursor, QTextOption
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog,
    QFrame, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from codex_backend import Backend


STYLE = """
QDialog { background: #faf8fd; color: #403951; }
QWidget { color: #403951; font-family: "Noto Sans CJK SC", "Noto Sans", sans-serif; font-size: 13px; }
QFrame#hero { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f2ebfb, stop:0.58 #f9f5fd, stop:1 #edf1fc); border: 1px solid #e6dcf1; border-radius: 16px; }
QLabel#eyebrow { color: #987ab9; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QLabel#heroTitle { color: #4c395e; font-size: 19px; font-weight: 700; }
QLabel#heroSub { color: #80738e; font-size: 12px; }
QLabel#avatar { background: #faf6ff; border: 2px solid #dfd1f0; border-radius: 29px; }
QTabWidget::pane { border: 0; background: transparent; }
QTabBar::tab { color: #8d829a; padding: 8px 18px; margin-right: 8px; border-bottom: 2px solid transparent; font-weight: 600; }
QTabBar::tab:selected { color: #715491; border-bottom: 2px solid #ac8cd0; }
QTabBar::tab:hover:!selected { color: #715491; }
QFrame#card { background: #ffffff; border: 1px solid #eae5f1; border-radius: 12px; }
QLabel#muted { color: #8b8196; font-size: 11px; }
QLabel#sectionTitle { color: #50405f; font-size: 15px; font-weight: 700; }
QLabel#notice { color: #7c7089; font-size: 11px; }
QLabel#status { color: #8a7b99; padding: 4px 2px; font-size: 11px; }
QComboBox, QPlainTextEdit { background: #f9f6fc; color: #473b55; border: 1px solid #e3d9ed; border-radius: 8px; padding: 8px; selection-background-color: #cbb5e5; }
QComboBox { padding-right: 26px; min-height: 20px; }
QComboBox::drop-down { width: 23px; border: 0; }
QComboBox QAbstractItemView { background: #ffffff; color: #51415e; selection-background-color: #e9dff4; border: 1px solid #e0d3ed; }
QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #b79bd4; }
QPushButton { background: #f3eef8; color: #655574; border: 1px solid #e8dff1; border-radius: 8px; padding: 8px 13px; font-weight: 600; }
QPushButton:hover { background: #ebe1f4; border-color: #d5c3e6; }
QPushButton:pressed { background: #e2d5ef; }
QPushButton:disabled { color: #b9afc0; background: #f5f2f7; border-color: #ede8f1; }
QPushButton#compact { padding: 4px 10px; min-height: 20px; }
QPushButton#primary { background: #a98bcf; color: white; border: 0; padding: 9px 20px; }
QPushButton#primary:hover { background: #9978c4; }
QPushButton#primary:disabled { background: #dcd2e8; color: #f5f1f8; }
QScrollArea#conversation { background: #fffdfd; border: 1px solid #eae4f0; border-radius: 12px; }
QScrollArea#limitsScroll { background: transparent; border: 0; }
QWidget#limitsContent { background: #faf8fd; }
QWidget#messages { background: #fffdfd; }
QTextEdit#assistantBubble { color: #4c4056; background: #f3eef9; border: 1px solid #ebe2f3; border-radius: 12px; padding: 11px 13px; }
QTextEdit#userBubble { color: #ffffff; background: #a88dcc; border: 1px solid #a88dcc; border-radius: 12px; padding: 11px 13px; }
QLabel#bubbleName { color: #998ba5; font-size: 10px; font-weight: 600; }
QProgressBar { background: #eee8f4; border: 0; border-radius: 4px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #ad91db; border-radius: 4px; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 3px; }
QScrollBar::handle:vertical { background: #d1c2dd; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


class ModeSelector(QComboBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#88749a'), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x, y = self.width() - 16, self.height() // 2 - 2
        painter.drawLine(x - 4, y, x, y + 4)
        painter.drawLine(x, y + 4, x + 4, y)


class StatusLabel(QLabel):
    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)


class MessageBubble(QTextEdit):
    """Read-only, selectable text that wraps long tokens and grows with its content."""

    def __init__(self, text, role):
        super().__init__()
        self.setObjectName('userBubble' if role == 'user' else 'assistantBubble')
        self.setReadOnly(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(self._resize_to_document)
        self.setPlainText(text)
        self._resize_to_document()

    def _resize_to_document(self, size=None):
        self.setFixedHeight(max(44, math.ceil(self.document().size().height()) + 24))

    def append_text(self, text):
        selection = self.textCursor()
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(str(text))
        self.setTextCursor(selection)



class ChatTranscript(QScrollArea):
    """Selectable plain-text bubbles with one continuously updated response."""

    def __init__(self):
        super().__init__()
        self.setObjectName('conversation')
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName('messages')
        self.messages = QVBoxLayout(self.content)
        self.messages.setContentsMargins(16, 18, 16, 18)
        self.messages.setSpacing(14)
        self.messages.addStretch(1)
        self.setWidget(self.content)
        self.bubbles = []
        self.response = None
        self.follow_latest = True
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)
        self.verticalScrollBar().valueChanged.connect(self._position_changed)
        self.add_message('assistant', '你好呀，我在這裡。想聊聊天，或一起處理什麼事情？')

    def add_message(self, role, text):
        self._remember_scroll_position()
        group = QWidget()
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(5)
        name = QLabel('你' if role == 'user' else '✦  CODEX')
        name.setObjectName('bubbleName')
        bubble = MessageBubble(text, role)
        box.addWidget(name)
        box.addWidget(bubble)
        row = QHBoxLayout()
        if role == 'user':
            row.addStretch(1)
        row.addWidget(group, 4)
        if role != 'user':
            row.addStretch(1)
        self.messages.insertLayout(self.messages.count() - 1, row)
        self.bubbles.append(bubble)
        return bubble

    def start_exchange(self, text):
        self.add_message('user', text)
        self.response = self.add_message('assistant', '')

    def append_response(self, text):
        if self.response is None:
            self.response = self.add_message('assistant', '')
        self._remember_scroll_position()
        self.response.append_text(text)

    def _remember_scroll_position(self):
        bar = self.verticalScrollBar()
        self.follow_latest = bar.value() >= bar.maximum() - 8

    def _range_changed(self, minimum, maximum):
        if self.follow_latest:
            self.verticalScrollBar().setValue(maximum)

    def _position_changed(self, value):
        self.follow_latest = value >= self.verticalScrollBar().maximum() - 8

    def toPlainText(self):
        return '\n'.join(bubble.toPlainText() for bubble in self.bubbles)


class _EventBridge(QObject):
    received = pyqtSignal(str, object)


class PetPanel(QDialog):
    """One panel is shared by both pets; all widgets stay on the Qt thread."""

    limits_changed = pyqtSignal(str, object)

    def __init__(self, action_callback=None, parent=None, backend_factory=Backend):
        super().__init__(parent)
        self.setObjectName('petPanel')
        self.setWindowTitle('Codex Pet · 對話與額度')
        self.resize(650, 780)
        self.setMinimumSize(480, 580)
        self.setStyleSheet(STYLE)
        self.action_callback = action_callback or (lambda action: None)
        self.cwd = None
        self.busy = False
        self.limits_inflight = False
        self.last_error = None
        self.bridge = _EventBridge(self)
        self.bridge.received.connect(self._event)
        self.backend = backend_factory(self.bridge.received.emit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(10)
        layout.addWidget(self._hero())
        self.tabs = QTabWidget(self)
        self._build_chat()
        self._build_limits()
        layout.addWidget(self.tabs, 1)
        self.status = StatusLabel()
        self.status.setText('✦  尚未連接。請先登入 ChatGPT。')
        self.status.setMaximumHeight(38)
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.backend.close)

    def _hero(self):
        hero = QFrame()
        hero.setObjectName('hero')
        row = QHBoxLayout(hero)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(15)
        avatar = QLabel()
        avatar.setObjectName('avatar')
        avatar.setFixedSize(58, 58)
        avatar.setAlignment(Qt.AlignCenter)
        directory = Path(__file__).resolve().parent
        image = directory / 'dragon-seated.png'
        if not image.is_file():
            image = directory.parent / 'assets' / 'dragon-seated.png'
        pixmap = QPixmap(str(image))
        if not pixmap.isNull():
            side = int(pixmap.width() * .18)
            portrait = pixmap.copy(int(pixmap.width() * .485), int(pixmap.height() * .015), side, side)
            avatar.setPixmap(portrait.scaled(52, 52, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        row.addWidget(avatar)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        eyebrow = QLabel('✦  你的桌面夥伴')
        eyebrow.setObjectName('eyebrow')
        title = QLabel('Codex Pet')
        title.setObjectName('heroTitle')
        subtitle = QLabel('聊聊靈感，也陪你完成每一件事。')
        subtitle.setObjectName('heroSub')
        titles.addWidget(eyebrow)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        row.addLayout(titles, 1)
        return hero

    @staticmethod
    def _card():
        card = QFrame()
        card.setObjectName('card')
        return card

    def _build_chat(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)
        card = self._card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 7, 12, 7)
        card_layout.setSpacing(4)
        row = QHBoxLayout()
        mode_label = QLabel('對話模式')
        mode_label.setObjectName('muted')
        row.addWidget(mode_label)
        self.mode = ModeSelector()
        for label, value in (('普通對話 · ChatGPT 計劃', 'chat'),
                             ('計劃 · ChatGPT 計劃', 'plan'),
                             ('任務 · Codex', 'task')):
            self.mode.addItem(label, value)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        row.addWidget(self.mode, 1)
        self.login_button = QPushButton('登入 ChatGPT')
        self.login_button.setObjectName('compact')
        self.login_button.clicked.connect(self.backend_login)
        row.addWidget(self.login_button)
        card_layout.addLayout(row)
        self.notice = QLabel()
        self.notice.setObjectName('notice')
        self.notice.setWordWrap(True)
        card_layout.addWidget(self.notice)
        project = QHBoxLayout()
        self.project_label = QLabel('工作目錄：尚未選擇')
        self.project_label.setObjectName('muted')
        self.project_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.project_label.setWordWrap(True)
        project.addWidget(self.project_label, 1)
        choose = QPushButton('選擇目錄')
        choose.setObjectName('compact')
        choose.clicked.connect(self._choose_cwd)
        project.addWidget(choose)
        card_layout.addLayout(project)
        layout.addWidget(card)
        self.transcript = ChatTranscript()
        layout.addWidget(self.transcript, 1)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText('寫下想說的話，或者描述一個任務…')
        self.prompt.setMinimumHeight(54)
        self.prompt.setMaximumHeight(72)
        layout.addWidget(self.prompt)
        buttons = QHBoxLayout()
        hint = QLabel('✦  隨時可以和我說話')
        hint.setObjectName('muted')
        buttons.addWidget(hint)
        buttons.addStretch(1)
        self.cancel_button = QPushButton('停止')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.backend_cancel)
        self.send_button = QPushButton('發送  ↗')
        self.send_button.setObjectName('primary')
        self.send_button.clicked.connect(self._send)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)
        self.tabs.addTab(page, '對話與任務')
        self._mode_changed()

    def _build_limits(self):
        page = QWidget()
        page.setObjectName('limitsContent')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)
        heading = QLabel('帳戶與用量')
        heading.setObjectName('sectionTitle')
        layout.addWidget(heading)
        description = QLabel('查看目前授權帳戶及 Codex 回傳的用量窗口。')
        description.setObjectName('muted')
        description.setWordWrap(True)
        layout.addWidget(description)
        account = self._card()
        account_layout = QVBoxLayout(account)
        account_layout.setContentsMargins(17, 16, 17, 16)
        account_layout.setSpacing(12)
        account_heading = QLabel('✦  已連接的帳戶')
        account_heading.setObjectName('sectionTitle')
        account_layout.addWidget(account_heading)
        self.account_text = QLabel('ChatGPT 授權：尚未查詢')
        self.account_text.setWordWrap(True)
        self.codex_account_text = QLabel('Codex 帳戶：尚未查詢')
        self.codex_account_text.setWordWrap(True)
        account_layout.addWidget(self.account_text)
        account_layout.addWidget(self.codex_account_text)
        layout.addWidget(account)
        usage = self._card()
        usage_layout = QVBoxLayout(usage)
        usage_layout.setContentsMargins(17, 16, 17, 16)
        usage_layout.setSpacing(12)
        usage_heading = QLabel('◷  Codex 額度')
        usage_heading.setObjectName('sectionTitle')
        usage_layout.addWidget(usage_heading)
        self.limit_text = QLabel('Codex 額度：尚未查詢')
        self.limit_text.setWordWrap(True)
        self.limit_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        usage_layout.addWidget(self.limit_text)
        self.usage_bars = []
        for _ in range(2):
            metric = QWidget()
            metric_layout = QVBoxLayout(metric)
            metric_layout.setContentsMargins(0, 1, 0, 1)
            metric_layout.setSpacing(5)
            caption = QLabel()
            caption.setObjectName('muted')
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(8)
            metric_layout.addWidget(caption)
            metric_layout.addWidget(bar)
            usage_layout.addWidget(metric)
            metric.hide()
            self.usage_bars.append((metric, caption, bar))
        layout.addWidget(usage)
        layout.addStretch(1)
        row = QHBoxLayout()
        refresh = QPushButton('刷新額度')
        refresh.setObjectName('primary')
        refresh.clicked.connect(lambda: self.refresh_limits())
        manage = QPushButton('管理 ChatGPT 用量')
        manage.clicked.connect(lambda: QDesktopServices.openUrl(QUrl('https://chatgpt.com/#settings/Usage')))
        row.addWidget(refresh)
        row.addWidget(manage)
        row.addStretch(1)
        layout.addLayout(row)
        scroll = QScrollArea()
        scroll.setObjectName('limitsScroll')
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, '帳戶與額度')

    def open_page(self, page='chat'):
        self.tabs.setCurrentIndex(1 if page == 'limits' else 0)
        self.show()
        self.raise_()
        self.activateWindow()
        if page == 'limits':
            self.refresh_limits()

    def backend_login(self):
        self.status.setText('正在啟動 ChatGPT 授權…')
        self.backend.login()

    def refresh_limits(self, include_account=True):
        if self.limits_inflight:
            return False
        self.limits_inflight = True
        self.limits_changed.emit('loading', None)
        self.action_callback('quota')
        self.status.setText('正在查詢 Codex 額度…')
        if include_account:
            self.backend.account()
        self.backend.limits()
        return True

    def backend_cancel(self):
        self.backend.cancel()

    def _mode_changed(self):
        mode = self.mode.currentData()
        notices = {
            'chat': '普通對話使用 ChatGPT 授權，無本地工具。',
            'plan': '計劃使用 ChatGPT 授權及只讀工作目錄；用量可能與 Codex 共享。',
            'task': '任務使用已登入的 Codex ChatGPT 帳戶；可依設定執行工具與請求審批。',
        }
        self.notice.setText(notices[mode])

    def _choose_cwd(self):
        selected = QFileDialog.getExistingDirectory(self, '選擇工作目錄', self.cwd or str(Path.home()))
        if selected:
            self.cwd = selected
            self.project_label.setText(selected)

    def _send(self):
        text = self.prompt.toPlainText().strip()
        mode = self.mode.currentData()
        if not text or self.busy:
            return
        if mode != 'chat' and not self.cwd:
            QMessageBox.information(self, '選擇工作目錄', '計劃和任務需要先選擇工作目錄。')
            return
        self.busy = True
        self.last_error = None
        self.send_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.prompt.clear()
        self.transcript.start_exchange(text)
        if mode in ('task', 'plan'):
            self.action_callback('work')
        self.backend.send(mode, text, self.cwd)

    def _append(self, text):
        self.transcript.append_response(text)

    def _event(self, event, data):
        if event == 'delta':
            self._append(data)
        elif event == 'status':
            self.status.setText(str(data))
        elif event == 'error':
            self.status.setText(str(data))
            if self.busy:
                self._append('\n[錯誤] ' + str(data) + '\n')
                self.last_error = str(data)
            else:
                self.transcript.add_message('assistant', '[錯誤] ' + str(data))
        elif event in ('account_error', 'login_error'):
            self.status.setText(str(data))
        elif event == 'done':
            result = data if isinstance(data, dict) else {}
            if result.get('status') == 'completed':
                self.status.setText('完成')
            elif self.last_error:
                self.status.setText('請求失敗：' + self.last_error)
            else:
                self.status.setText('已結束：' + str(result.get('status', '未知')))
            if result.get('status') == 'completed' and result.get('mode') == 'task':
                self.action_callback('celebrate')
            else:
                self.action_callback('idle')
            self._idle()
        elif event == 'account':
            account = data if isinstance(data, dict) else {}
            info = account.get('account') or account
            info = info if isinstance(info, dict) else {}
            target = self.codex_account_text if account.get('source') == 'Codex' else self.account_text
            label = 'Codex 帳戶：' if target is self.codex_account_text else 'ChatGPT 授權：'
            target.setText(label + str(info.get('email') or info.get('type') or '不可用') +
                           '　方案：' + str(info.get('planType') or '不可用'))
        elif event == 'limits':
            self.limits_inflight = False
            self._show_limits(data)
            self.limits_changed.emit('limits', data)
        elif event == 'limits_error':
            self.limits_inflight = False
            self.limit_text.setText('Codex 額度查詢失敗：' + str(data))
            for widget, _, _ in self.usage_bars:
                widget.hide()
            self.status.setText('額度查詢失敗：' + str(data))
            self.limits_changed.emit('limits_error', data)
        elif event == 'approval':
            self._approval(data)

    def _idle(self):
        self.busy = False
        self.send_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _show_limits(self, data):
        data = data if isinstance(data, dict) else {}
        entries = data.get('rateLimitsByLimitId') or {}
        if not isinstance(entries, dict):
            entries = {}
        if not entries and data.get('rateLimits'):
            entries = {'codex': data['rateLimits']}
        lines = []
        metrics = []
        for name, limit in entries.items():
            if not isinstance(limit, dict):
                continue
            for key, label in (('primary', '主要窗口'), ('secondary', '次要窗口')):
                window = limit.get(key)
                if not isinstance(window, dict):
                    continue
                used = window.get('usedPercent')
                minutes = window.get('windowDurationMins')
                reset = window.get('resetsAt')
                try:
                    reset_text = datetime.fromtimestamp(reset).strftime('%Y-%m-%d %H:%M') if reset is not None else '不可用'
                except (TypeError, ValueError, OSError, OverflowError):
                    reset_text = '不可用'
                lines.append(f'{name} {label}：已用 {used if used is not None else "不可用"}%　窗口 {minutes if minutes is not None else "不可用"} 分鐘　重置 {reset_text}')
                if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) and 0 <= used <= 100:
                    metrics.append((f'{name} · {label}  {used}%', round(used)))
        self.limit_text.setText('\n'.join(lines) if lines else 'Codex 額度：不可用')
        for index, (widget, caption, bar) in enumerate(self.usage_bars):
            if index < len(metrics):
                caption.setText(metrics[index][0])
                bar.setValue(metrics[index][1])
                widget.show()
            else:
                widget.hide()

    def _approval(self, data):
        if not isinstance(data, dict) or 'id' not in data:
            return
        method = str(data.get('method', ''))
        if method == 'item/tool/requestUserInput':
            answers = {}
            questions = data.get('params', {}).get('questions', [])
            for question in questions:
                options = question.get('options') or []
                labels = [str(option.get('label', '')) for option in options if isinstance(option, dict)]
                if labels:
                    value, ok = QInputDialog.getItem(self, 'Codex 需要你的回答',
                                                     str(question.get('question', '')), labels, 0, True)
                else:
                    echo = QLineEdit.Password if question.get('isSecret') else QLineEdit.Normal
                    value, ok = QInputDialog.getText(self, 'Codex 需要你的回答',
                                                     str(question.get('question', '')), echo)
                if not ok:
                    self.backend.approve(data['id'], 'cancel')
                    return
                answers[str(question.get('id', ''))] = {'answers': [str(value)]}
            self.backend.approve(data['id'], {'answers': answers})
            return
        if method not in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                          'item/permissions/requestApproval'):
            self.backend.approve(data['id'], 'decline')
            self.status.setText('不支援的審批請求已拒絕：' + method)
            return
        details = json.dumps(data.get('params', {}), ensure_ascii=False, indent=2, default=str)
        dialog = QDialog(self)
        dialog.setWindowTitle('Codex 請求審批')
        dialog.resize(600, 400)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('請檢查完整請求後決定是否允許：'))
        body = QPlainTextEdit(dialog)
        body.setReadOnly(True)
        body.setPlainText(details)
        layout.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No, parent=dialog)
        buttons.button(QDialogButtonBox.No).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        answer = dialog.exec_()
        self.backend.approve(data['id'], 'accept' if answer == QDialog.Accepted else 'decline')

    def closeEvent(self, event):
        self.hide()
        event.ignore()
