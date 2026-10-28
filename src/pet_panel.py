"""Shared Codex Pet conversation and usage window."""
import json
import math
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QPainter, QPen, QPixmap, QTextCursor, QTextOption
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog,
    QFrame, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from codex_backend import Backend, MAX_IMAGE_BYTES, MAX_IMAGE_COUNT, MAX_IMAGE_TOTAL_BYTES


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
QListWidget#imageList { background: #f9f6fc; border: 1px solid #e3d9ed; border-radius: 8px; padding: 3px; }
QListWidget#imageList::item:selected { background: #e4d7f1; color: #473b55; }
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

    def clear(self):
        while self.messages.count() > 1:
            row = self.messages.takeAt(0).layout()
            if row:
                while row.count():
                    item = row.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
                row.deleteLater()
        self.bubbles = []
        self.response = None
        self.follow_latest = True


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
        self.active_mode = None
        self.limits_inflight = False
        self.last_error = None
        self.model_cache = {}
        self.model_choice = {}
        self.effort_choice = {}
        self.models_loading = set()
        self.model_generation = {mode: 0 for mode in ('chat', 'plan', 'task')}
        self.login_inflight = False
        self.selected_thread = None
        self.thread_dialog = None
        self.thread_loading = False
        self.thread_cursor = None
        self.thread_reading = None
        self.images = []
        self.images_after_login_need_review = False
        self.inflight_draft = None
        self.failed_drafts = []
        self.progress_entries = []
        self.progress_plans = {}
        self.progress_mode = None
        self.bridge = _EventBridge(self)
        self.bridge.received.connect(self._event)
        self.backend = backend_factory(self.bridge.received.emit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(10)
        self.hero = self._hero()
        layout.addWidget(self.hero)
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
        options = QHBoxLayout()
        options.addWidget(QLabel('模型'))
        self.model = ModeSelector()
        self.model.currentIndexChanged.connect(self._model_changed)
        options.addWidget(self.model, 1)
        self.model_refresh = QPushButton('刷新模型')
        self.model_refresh.setObjectName('compact')
        self.model_refresh.clicked.connect(self._refresh_models)
        options.addWidget(self.model_refresh)
        card_layout.addLayout(options)
        model_options = QHBoxLayout()
        self.effort_label = QLabel('推理強度')
        self.effort = ModeSelector()
        self.effort.currentIndexChanged.connect(self._effort_changed)
        model_options.addWidget(self.effort_label)
        model_options.addWidget(self.effort, 1)
        self.new_button = QPushButton('新對話')
        self.new_button.setObjectName('compact')
        self.new_button.clicked.connect(self._new_conversation)
        model_options.addWidget(self.new_button)
        self.threads_button = QPushButton('選擇 Codex 對話')
        self.threads_button.setObjectName('compact')
        self.threads_button.clicked.connect(self._open_threads)
        model_options.addWidget(self.threads_button)
        card_layout.addLayout(model_options)
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
        self.choose_cwd = QPushButton('選擇目錄')
        self.choose_cwd.setObjectName('compact')
        self.choose_cwd.clicked.connect(self._choose_cwd)
        project.addWidget(self.choose_cwd)
        card_layout.addLayout(project)
        permission = QHBoxLayout()
        self.permission_label = QLabel('本次權限')
        self.permission = ModeSelector()
        self.permission.addItem('請選擇本次權限', None)
        self.permission.addItem('只讀', 'read-only')
        self.permission.addItem('工作目錄可寫', 'workspace-write')
        self.permission.setToolTip('只作用於本次桌寵執行；不代表繼承原對話權限。需要時會請求審批。')
        permission.addWidget(self.permission_label)
        permission.addWidget(self.permission, 1)
        card_layout.addLayout(permission)
        self.permission_notice = QLabel('僅適用本次執行，不代表繼承原對話權限；需要時請求審批。')
        self.permission_notice.setObjectName('notice')
        self.permission_notice.setWordWrap(True)
        card_layout.addWidget(self.permission_notice)
        self.options_scroll = QScrollArea()
        self.options_scroll.setWidgetResizable(True)
        self.options_scroll.setFrameShape(QFrame.NoFrame)
        self.options_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.options_scroll.setMaximumHeight(250)
        self.options_scroll.setMinimumHeight(120)
        self.options_scroll.setWidget(card)
        layout.addWidget(self.options_scroll)
        self.transcripts = {mode: ChatTranscript() for mode in ('chat', 'plan', 'task')}
        self.transcript_stack = QStackedWidget()
        for transcript in self.transcripts.values():
            self.transcript_stack.addWidget(transcript)
        self.transcript = self.transcripts['chat']
        layout.addWidget(self.transcript_stack, 1)
        self.progress_panel = QWidget()
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(3)
        self.progress_toggle = QPushButton('執行進度 ▾')
        self.progress_toggle.setObjectName('compact')
        self.progress_toggle.clicked.connect(self._toggle_progress)
        progress_layout.addWidget(self.progress_toggle)
        self.progress_log = QPlainTextEdit()
        self.progress_log.setReadOnly(True)
        self.progress_log.setMaximumHeight(110)
        self.progress_log.setMinimumHeight(55)
        progress_layout.addWidget(self.progress_log)
        self.progress_panel.hide()
        layout.addWidget(self.progress_panel)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText('寫下想說的話，或者描述一個任務…')
        self.prompt.setMinimumHeight(54)
        self.prompt.setMaximumHeight(72)
        layout.addWidget(self.prompt)
        self.attachments = QWidget()
        attachment_row = QHBoxLayout(self.attachments)
        attachment_row.setContentsMargins(0, 0, 0, 0)
        attachment_content = QVBoxLayout()
        attachment_content.setSpacing(2)
        self.upload_hint = QLabel()
        self.upload_hint.setObjectName('muted')
        self.upload_hint.setWordWrap(True)
        attachment_content.addWidget(self.upload_hint)
        self.attachment_review = QLabel('登入已更新；附件仍在草稿中。發送前請確認帳戶和圖片。')
        self.attachment_review.setObjectName('notice')
        self.attachment_review.setWordWrap(True)
        self.attachment_review.hide()
        attachment_content.addWidget(self.attachment_review)
        self.image_list = QListWidget()
        self.image_list.setObjectName('imageList')
        self.image_list.setMaximumHeight(54)
        self.image_list.setAccessibleName('本次發送的圖片')
        attachment_content.addWidget(self.image_list)
        attachment_row.addLayout(attachment_content, 1)
        self.remove_image = QPushButton('移除所選')
        self.remove_image.setObjectName('compact')
        self.remove_image.clicked.connect(self._remove_image)
        attachment_row.addWidget(self.remove_image)
        self.attachments.hide()
        layout.addWidget(self.attachments)
        buttons = QHBoxLayout()
        self.add_image = QPushButton('＋ 圖片')
        self.add_image.setObjectName('compact')
        self.add_image.clicked.connect(self._choose_images)
        buttons.addWidget(self.add_image)
        self.restore_draft = QPushButton('恢復失敗草稿')
        self.restore_draft.setObjectName('compact')
        self.restore_draft.setToolTip('把先前失敗的內容加回目前草稿，不會覆蓋目前輸入。')
        self.restore_draft.clicked.connect(self._restore_failed_draft)
        self.restore_draft.hide()
        buttons.addWidget(self.restore_draft)
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
        elif self.mode.currentData() not in self.model_cache:
            self._refresh_models()

    def backend_login(self):
        if self.busy or self.login_inflight:
            return
        self.login_inflight = True
        self.login_button.setEnabled(False)
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
        if self.busy:
            self.cancel_button.setEnabled(False)
            self.status.setText('正在請求停止，等待 Codex 確認結果…')
        self.backend.cancel()

    def _mode_changed(self):
        mode = self.mode.currentData()
        if self.busy and mode != self.active_mode:
            self.mode.blockSignals(True)
            self.mode.setCurrentIndex(self.mode.findData(self.active_mode))
            self.mode.blockSignals(False)
            self.status.setText('正在執行，請先停止或等待完成再切換模式。')
            return
        notices = {
            'chat': '普通對話使用 ChatGPT 授權，無本地工具。圖片加入上下文後，後續請求會再次傳送，直至新對話。',
            'plan': '計劃使用 ChatGPT 授權及只讀工作目錄；用量可能與 Codex 共享。',
            'task': '任務使用已登入的 Codex ChatGPT 帳戶；可依設定執行工具與請求審批。',
        }
        self.notice.setText(notices[mode] + ' 各模式各自保留上下文；不會同步 ChatGPT 網站對話。')
        image_notice = ('普通對話：發送的圖片會隨後續請求再次傳送，直至新對話。'
                        if mode == 'chat' else
                        '計劃／任務：圖片由 Codex 線程管理；臨時副本在執行結束後清理。')
        self.upload_hint.setText(image_notice)
        self.add_image.setToolTip(
            '選擇本機圖片，按發送時上傳。' + image_notice +
            f'最多 {MAX_IMAGE_COUNT} 張，單張 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB，'
            f'總計 {MAX_IMAGE_TOTAL_BYTES // (1024 * 1024)} MiB。')
        if hasattr(self, 'transcripts'):
            self.transcript = self.transcripts[mode]
            self.transcript_stack.setCurrentWidget(self.transcript)
        self.threads_button.setVisible(mode == 'task')
        self.effort.setVisible(mode == 'task')
        self.effort_label.setVisible(mode == 'task')
        self._show_permission()
        if hasattr(self, 'progress_panel'):
            self.progress_panel.setVisible(mode == self.progress_mode)
        self._show_models(mode)
        if hasattr(self, 'status') and mode not in self.model_cache:
            self._refresh_models()

    def _refresh_models(self):
        self._request_models(self.mode.currentData())

    def _request_models(self, mode):
        if self.busy or mode in self.models_loading:
            return
        self.models_loading.add(mode)
        self.model_generation[mode] += 1
        if mode == self.mode.currentData():
            self.model_refresh.setEnabled(False)
            self.status.setText('正在查詢可用模型…')
        self.backend.models(mode, self.model_generation[mode])

    def _show_models(self, mode):
        self.model.blockSignals(True)
        self.model.clear()
        entries = self.model_cache.get(mode, [])
        if mode == 'task' and self.selected_thread:
            self.model.addItem('沿用所選對話的模型與強度', {'id': None, 'follow_thread': True})
        for item in entries:
            self.model.addItem(item.get('name') or item['id'], item)
        choice = self.model_choice.get(mode)
        index = next((i for i in range(self.model.count())
                      if isinstance(self.model.itemData(i), dict)
                      and (('__thread__' if self.model.itemData(i).get('follow_thread')
                            else self.model.itemData(i).get('id')) == choice)), -1)
        if index >= 0:
            self.model.setCurrentIndex(index)
        elif mode == 'task' and self.selected_thread:
            self.model.setCurrentIndex(0)
        elif entries:
            default = next((i for i, m in enumerate(entries) if m.get('default')), 0)
            self.model.setCurrentIndex(default)
        else:
            self.model.addItem('尚無模型，請刷新', None)
        self.model.blockSignals(False)
        self.model_refresh.setEnabled(not self.busy and mode not in self.models_loading)
        self._model_changed()

    def _model_changed(self):
        item = self.model.currentData() or {}
        mode = self.mode.currentData()
        model_id = '__thread__' if item.get('follow_thread') else item.get('id')
        if model_id:
            self.model_choice[mode] = model_id
        self.effort.blockSignals(True)
        self.effort.clear()
        efforts = item.get('efforts') or []
        default = item.get('default_effort')
        self.effort.addItem('默認' + ('（' + default + '）' if default else ''), None)
        for effort in efforts:
            self.effort.addItem(effort, effort)
        remembered = self.effort_choice.get((mode, model_id))
        if remembered in efforts:
            self.effort.setCurrentIndex(self.effort.findData(remembered))
        self.effort.blockSignals(False)

    def _effort_changed(self):
        item = self.model.currentData() or {}
        model_id = '__thread__' if item.get('follow_thread') else item.get('id')
        if model_id:
            self.effort_choice[(self.mode.currentData(), model_id)] = self.effort.currentData()

    def _show_permission(self):
        visible = self.mode.currentData() == 'task' and bool(self.selected_thread)
        self.permission_label.setVisible(visible)
        self.permission.setVisible(visible)
        self.permission_notice.setVisible(visible)

    def _new_conversation(self):
        if self.busy:
            return
        mode = self.mode.currentData()
        self.backend.new_thread(mode, self.cwd)
        self.transcript.clear()
        self.transcript.add_message('assistant', '新的對話已開始。')
        if mode == 'task':
            self.selected_thread = None
            if self.model_choice.get('task') == '__thread__':
                self.model_choice.pop('task')
            self.permission.setCurrentIndex(0)
            self._show_permission()
            self._show_models(mode)
        self.status.setText('已開始新對話；原有 Codex 歷史不會刪除。')

    def _choose_cwd(self):
        if self.busy:
            return
        selected = QFileDialog.getExistingDirectory(self, '選擇工作目錄', self.cwd or str(Path.home()))
        if selected:
            if selected != self.cwd:
                self.selected_thread = None
                if self.model_choice.get('task') == '__thread__':
                    self.model_choice.pop('task')
                self.permission.setCurrentIndex(0)
                self._show_permission()
                if self.mode.currentData() == 'task':
                    self._show_models('task')
                for mode in ('plan', 'task'):
                    self.backend.new_thread(mode, selected)
                    self.transcripts[mode].clear()
            self.cwd = selected
            self.project_label.setText(selected)

    def _choose_images(self):
        if self.busy or self.login_inflight:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, '選擇要隨本次對話上傳的圖片', str(Path.home()),
            '圖片 (*.png *.PNG *.jpg *.JPG *.jpeg *.JPEG *.webp *.WEBP *.gif *.GIF)')
        added = 0
        over_limit = False
        for path in paths:
            if path in self.images:
                continue
            if len(self.images) >= MAX_IMAGE_COUNT:
                over_limit = True
                break
            self.images.append(path)
            added += 1
        self._show_images()
        if over_limit:
            self.status.setText(f'一次最多附加 {MAX_IMAGE_COUNT} 張圖片，請先移除不需要的圖片。')
        elif added:
            self.status.setText('圖片已選擇；按發送時上傳。' + self.upload_hint.text())

    def _show_images(self):
        self.image_list.clear()
        for path in self.images:
            self.image_list.addItem(Path(path).name)
        self.attachments.setVisible(bool(self.images))
        if not self.images:
            self.images_after_login_need_review = False
        self.attachment_review.setVisible(bool(self.images) and self.images_after_login_need_review)
        self.remove_image.setEnabled(not self.busy and bool(self.images))

    def _remove_image(self):
        row = self.image_list.currentRow()
        if 0 <= row < len(self.images) and not self.busy:
            del self.images[row]
            self._show_images()

    def _update_failed_drafts(self):
        self.restore_draft.setVisible(bool(self.failed_drafts))
        if self.failed_drafts:
            self.restore_draft.setText(f'恢復失敗草稿 ({len(self.failed_drafts)})')
        self.restore_draft.setEnabled(not self.busy)

    def _restore_failed_draft(self):
        if self.busy or not self.failed_drafts:
            return
        draft = self.failed_drafts[0]
        if (draft['mode'] != self.mode.currentData()
                or (draft['mode'] != 'chat' and draft['cwd'] != self.cwd)):
            self.status.setText('請切回原對話模式和工作目錄，再恢復這份草稿。')
            return
        images = self.images + [path for path in draft['images'] if path not in self.images]
        if len(images) > MAX_IMAGE_COUNT:
            self.status.setText('圖片數量超過上限，請先移除目前草稿中的圖片，再恢復。')
            return
        current = self.prompt.toPlainText()
        self.prompt.setPlainText(current + ('\n\n' if current else '') + draft['text'])
        self.images = images
        self.images_after_login_need_review = (self.images_after_login_need_review
                                                or bool(draft.get('review_after_login') and draft['images']))
        self.failed_drafts.pop(0)
        self._show_images()
        self._update_failed_drafts()
        self.status.setText('已恢復草稿。上次請求可能已部分處理，重試可能重複。')

    def _open_threads(self):
        if self.busy or self.thread_dialog is not None:
            return
        if self.thread_loading:
            self.status.setText('上一個對話查詢仍在進行，請稍候。')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('選擇 Codex 對話')
        dialog.setModal(True)
        dialog.resize(560, 420)
        layout = QVBoxLayout(dialog)
        note = QLabel('僅在你選擇後讀取完整對話；繼續任務會使用該對話的工作目錄。'
                      '若其他 Codex 窗口同時操作同一對話，可能造成衝突。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.thread_list = QListWidget(dialog)
        layout.addWidget(self.thread_list, 1)
        self.thread_status = QLabel('正在載入…')
        self.thread_status.setWordWrap(True)
        layout.addWidget(self.thread_status)
        row = QHBoxLayout()
        self.thread_more = QPushButton('載入更多')
        self.thread_more.clicked.connect(lambda: self._load_threads(self.thread_cursor))
        self.thread_open = QPushButton('讀取所選對話')
        self.thread_open.clicked.connect(self._read_thread)
        close = QPushButton('關閉')
        close.clicked.connect(dialog.reject)
        row.addWidget(self.thread_more)
        row.addStretch()
        row.addWidget(close)
        row.addWidget(self.thread_open)
        layout.addLayout(row)
        self.thread_dialog = dialog
        self.thread_cursor = None
        self.thread_reading = None
        dialog.finished.connect(self._threads_closed)
        dialog.show()
        self._load_threads(None)

    def _threads_closed(self):
        self.thread_dialog = None

    def _load_threads(self, cursor):
        if self.thread_loading or self.thread_dialog is None:
            return
        self.thread_loading = True
        self.thread_more.setText('載入更多')
        self.thread_more.setEnabled(False)
        self.thread_open.setEnabled(False)
        self.thread_status.setText('正在載入對話列表…')
        self.backend.codex_threads(cursor)

    def _read_thread(self):
        item = self.thread_list.currentItem()
        if self.thread_loading or item is None:
            return
        self.thread_reading = item.data(Qt.UserRole)
        self.thread_loading = True
        self.thread_more.setEnabled(False)
        self.thread_open.setEnabled(False)
        self.thread_status.setText('正在讀取所選對話…')
        self.backend.codex_thread(self.thread_reading)

    def _show_thread_list(self, result):
        if not self.thread_loading or self.thread_reading is not None:
            return
        if self.thread_dialog is None:
            self.thread_loading = False
            return
        from PyQt5.QtWidgets import QListWidgetItem
        for thread in result.get('data', []):
            if not isinstance(thread, dict) or not thread.get('id'):
                continue
            title = thread.get('name') or thread.get('preview') or thread['id']
            item = QListWidgetItem(str(title)[:160] + '\n' + str(thread.get('cwd') or '工作目錄不可用'))
            item.setData(Qt.UserRole, thread['id'])
            self.thread_list.addItem(item)
        self.thread_cursor = result.get('nextCursor')
        self.thread_loading = False
        self.thread_more.setEnabled(bool(self.thread_cursor))
        self.thread_open.setEnabled(True)
        self.thread_status.setText('請選擇對話後點擊讀取。' if self.thread_list.count() else '沒有找到 Codex 對話。')

    def _show_thread(self, result):
        if not self.thread_loading or not self.thread_reading:
            return
        if self.thread_dialog is None:
            self.thread_loading = False
            self.thread_reading = None
            return
        thread = result.get('thread') or {}
        if thread.get('id') != self.thread_reading or not thread.get('cwd'):
            self.thread_loading = False
            self.thread_reading = None
            self.thread_open.setEnabled(True)
            self.thread_status.setText('無法確認對話或工作目錄，請重試。')
            return
        if self.cwd != thread['cwd']:
            self.backend.new_thread('plan', thread['cwd'])
            self.transcripts['plan'].clear()
        transcript = self.transcripts['task']
        transcript.clear()
        for turn in thread.get('turns', []):
            for item in turn.get('items', []):
                kind = item.get('type')
                if kind == 'userMessage':
                    parts = []
                    for part in item.get('content', []):
                        if not isinstance(part, dict):
                            continue
                        if part.get('type') in ('image', 'localImage', 'input_image'):
                            parts.append('[圖片]')
                        elif part.get('text'):
                            parts.append(part['text'])
                    text = '\n'.join(part for part in parts if part)
                    if text:
                        transcript.add_message('user', text)
                elif kind == 'agentMessage' and item.get('text'):
                    transcript.add_message('assistant', item['text'])
                elif kind == 'plan' and item.get('text'):
                    transcript.add_message('assistant', '[歷史計劃]\n' + str(item['text']))
                elif kind in ('commandExecution', 'fileChange', 'webSearch',
                              'mcpToolCall', 'dynamicToolCall', 'contextCompaction'):
                    labels = {'commandExecution': '命令', 'fileChange': '文件變更',
                              'webSearch': '網絡搜索', 'mcpToolCall': 'MCP 工具',
                              'dynamicToolCall': '工具', 'contextCompaction': '上下文整理'}
                    summary = '[歷史執行] ' + labels[kind]
                    if item.get('status'):
                        summary += ' · ' + str(item['status'])
                    if kind == 'fileChange':
                        paths = [str(change['path']) for change in (item.get('changes') or [])
                                 if isinstance(change, dict) and change.get('path')]
                        if paths:
                            summary += '\n' + '\n'.join(paths[:20])
                            if len(paths) > 20:
                                summary += '\n…'
                    elif kind == 'mcpToolCall':
                        tool = ' / '.join(str(item[key]) for key in ('server', 'tool') if item.get(key))
                        if tool:
                            summary += '\n' + tool
                    transcript.add_message('assistant', summary)
        self.cwd = thread['cwd']
        self.project_label.setText(self.cwd)
        self.selected_thread = thread['id']
        self.model_choice['task'] = '__thread__'
        self.effort_choice = {key: val for key, val in self.effort_choice.items() if key[0] != 'task'}
        self.permission.setCurrentIndex(0)
        self._show_permission()
        QTimer.singleShot(0, lambda: self.options_scroll.ensureWidgetVisible(self.permission))
        self._show_models('task')
        self.thread_loading = False
        self.thread_reading = None
        self.thread_dialog.accept()
        self.status.setText('已讀取 Codex 對話；發送後會在此對話中繼續。')

    def _send(self):
        draft_text = self.prompt.toPlainText()
        text = draft_text.strip()
        mode = self.mode.currentData()
        if (not text and not self.images) or self.busy:
            return
        if self.login_inflight:
            self.status.setText('正在等待授權完成，請稍後再發送。')
            return
        if mode != 'chat' and not self.cwd:
            QMessageBox.information(self, '選擇工作目錄', '計劃和任務需要先選擇工作目錄。')
            return
        selected = self.model.currentData()
        if not selected:
            self.status.setText('沒有可選模型，請先刷新模型。')
            return
        if mode == 'task' and self.selected_thread and not self.permission.currentData():
            self.status.setText('請先選擇本次執行權限；不會自動沿用原對話權限。')
            self.options_scroll.ensureWidgetVisible(self.permission)
            self.permission.setFocus()
            return
        if self.images and self.images_after_login_need_review:
            names = '\n'.join(Path(path).name for path in self.images)
            confirmation = QMessageBox.question(
                self, '確認附件與登入帳戶',
                '登入已更新，請確認目前模式使用的帳戶，並確認要發送這些草稿圖片：\n\n' + names,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if confirmation != QMessageBox.Yes:
                return
            self.images_after_login_need_review = False
        self.busy = True
        self.active_mode = mode
        self.last_error = None
        self.send_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.mode.setEnabled(False)
        self.model.setEnabled(False)
        self.model_refresh.setEnabled(False)
        self.effort.setEnabled(False)
        self.new_button.setEnabled(False)
        self.threads_button.setEnabled(False)
        self.choose_cwd.setEnabled(False)
        self.permission.setEnabled(False)
        self.login_button.setEnabled(False)
        self.add_image.setEnabled(False)
        self.remove_image.setEnabled(False)
        self.restore_draft.setEnabled(False)
        self.progress_entries = []
        self.progress_plans = {}
        self.progress_log.clear()
        self.progress_panel.hide()
        self.prompt.clear()
        images = list(self.images)
        self.inflight_draft = {'mode': mode, 'cwd': self.cwd, 'text': draft_text, 'images': images}
        self.images.clear()
        self._show_images()
        names = '\n'.join(Path(path).name for path in images)
        self.transcript.start_exchange(text + ('\n\n[圖片附件]\n' + names if names else ''))
        if mode in ('task', 'plan'):
            self.action_callback('work')
        self.backend.send(mode, text, self.cwd, selected['id'],
                          self.effort.currentData() if mode == 'task' else None,
                          self.selected_thread if mode == 'task' else None,
                          self.permission.currentData() if mode == 'task' and self.selected_thread else None,
                          images or None)

    def _append(self, text):
        self.transcript.append_response(text)

    def _toggle_progress(self):
        self.progress_log.setVisible(self.progress_log.isHidden())
        self.progress_toggle.setText('執行進度 ▾' if not self.progress_log.isHidden() else '執行進度 ▸')

    def _progress(self, data):
        if not isinstance(data, dict):
            return
        kind = data.get('kind')
        if kind == 'item':
            labels = {'commandExecution': '命令', 'fileChange': '文件變更', 'webSearch': '網絡搜索',
                      'mcpToolCall': 'MCP 工具', 'dynamicToolCall': '工具', 'contextCompaction': '上下文整理'}
            phase = '開始' if data.get('phase') == 'started' else '結束'
            line = f'{phase}：{labels.get(data.get("type"), "工具")}'
            if data.get('status'):
                line += ' · ' + str(data['status'])
            if data.get('detail'):
                line += '\n' + str(data['detail'])[:4000]
            self.progress_entries.append(line)
            self.progress_entries = self.progress_entries[-200:]
        elif kind == 'model_rerouted':
            line = f'模型已變更：{data.get("from_model") or "未知"} → {data.get("to_model") or "未知"}'
            if data.get('reason'):
                line += '\n原因：' + str(data['reason'])[:4000]
            self.progress_entries.append(line)
            self.progress_entries = self.progress_entries[-200:]
        elif kind == 'plan':
            steps = data.get('steps') or []
            lines = ['執行計劃：']
            if data.get('explanation'):
                lines.append(str(data['explanation'])[:4000])
            for step in steps[:100]:
                if isinstance(step, dict):
                    lines.append(f'• {step.get("step", "")}（{step.get("status", "未知")}）')
            self.progress_plans['steps'] = '\n'.join(lines)
        elif kind in ('plan_delta', 'plan_final'):
            key = str(data.get('item_id') or 'plan')
            if kind == 'plan_delta':
                self.progress_plans[key] = (self.progress_plans.get(key, '') + str(data.get('delta') or ''))[-32000:]
            else:
                self.progress_plans[key] = str(data.get('text') or '')[-32000:]
        else:
            return
        self.progress_mode = self.active_mode or self.mode.currentData()
        self.progress_panel.show()
        bar = self.progress_log.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 8
        values = self.progress_entries + [text for text in self.progress_plans.values() if text]
        self.progress_log.setPlainText('\n\n'.join(values))
        if follow:
            bar.setValue(bar.maximum())

    def _event(self, event, data):
        if event == 'models':
            mode = data.get('mode')
            if mode not in self.model_generation or data.get('generation') != self.model_generation[mode]:
                return
            self.models_loading.discard(mode)
            entries = [item for item in data.get('models', []) if isinstance(item, dict) and item.get('id')]
            self.model_cache[mode] = entries
            if mode == self.mode.currentData():
                self._show_models(mode)
                if not self.busy:
                    self.status.setText('已更新可用模型。' if entries else '沒有可選模型，請刷新。')
        elif event == 'models_error':
            mode = data.get('mode')
            if mode not in self.model_generation or data.get('generation') != self.model_generation[mode]:
                return
            self.models_loading.discard(mode)
            if mode == self.mode.currentData():
                self._show_models(mode)
                self.status.setText('模型查詢失敗：' + str(data.get('message', '未知錯誤')))
        elif event == 'codex_threads':
            self._show_thread_list(data)
        elif event == 'codex_thread':
            self._show_thread(data)
        elif event in ('codex_threads_error', 'codex_thread_error'):
            self.thread_loading = False
            self.thread_reading = None
            if self.thread_dialog is not None:
                self.thread_status.setText('對話查詢失敗：' + str(data))
                self.thread_more.setText('重試')
                self.thread_more.setEnabled(True)
                self.thread_open.setEnabled(True)
            else:
                self.status.setText('對話查詢失敗：' + str(data))
        elif event == 'delta':
            self._append(data)
        elif event == 'progress':
            self._progress(data)
        elif event == 'status':
            self.status.setText(str(data))
        elif event == 'error':
            self.status.setText(str(data))
            if self.busy:
                self._append('\n[錯誤] ' + str(data) + '\n')
                self.last_error = str(data)
            else:
                self.transcript.add_message('assistant', '[錯誤] ' + str(data))
        elif event == 'login_complete':
            self.login_inflight = False
            self.login_button.setEnabled(not self.busy)
            if self.images:
                self.images_after_login_need_review = True
            for draft in self.failed_drafts:
                if draft['images']:
                    draft['review_after_login'] = True
            self._show_images()
            for mode in ('chat', 'plan'):
                self.model_generation[mode] += 1
                self.models_loading.discard(mode)
                self.model_cache.pop(mode, None)
                self.model_choice.pop(mode, None)
                self.backend.new_thread(mode, self.cwd)
                self.transcripts[mode].clear()
                self.transcripts[mode].add_message('assistant', '授權已更新，已開始新的對話。')
            if self.mode.currentData() in ('chat', 'plan'):
                self._show_models(self.mode.currentData())
            for mode in ('chat', 'plan'):
                self._request_models(mode)
        elif event in ('account_error', 'login_error'):
            if event == 'login_error':
                self.login_inflight = False
                self.login_button.setEnabled(not self.busy)
            self.status.setText(str(data))
        elif event == 'done':
            result = data if isinstance(data, dict) else {}
            outcome = result.get('status')
            if outcome == 'completed':
                self.status.setText('完成')
            elif outcome == 'unknown':
                message = '任務結果未知，無法確認是否已停止或完成。請先檢查 Codex 對話及工作目錄；重試可能重複執行。'
                self.status.setText(message)
                self.transcript.add_message('assistant', message)
            elif outcome == 'interrupted':
                self.status.setText('已確認停止；此前可能已有部分操作。')
            elif self.last_error:
                self.status.setText('請求失敗：' + self.last_error)
            else:
                self.status.setText('已結束：' + str(outcome or '未知'))
            draft = self.inflight_draft
            self.inflight_draft = None
            if outcome != 'completed' and draft:
                if not self.prompt.toPlainText() and not self.images:
                    self.prompt.setPlainText(draft['text'])
                    self.images = draft['images']
                    self._show_images()
                    self.status.setText(self.status.text() + '；草稿已恢復，上次請求可能已部分處理。')
                else:
                    self.failed_drafts.append(draft)
                    self.status.setText(self.status.text() + '；草稿可恢復，上次請求可能已部分處理。')
            if outcome == 'completed' and result.get('mode') == 'task':
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
        self.active_mode = None
        self.send_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.mode.setEnabled(True)
        self.model.setEnabled(True)
        self.model_refresh.setEnabled(self.mode.currentData() not in self.models_loading)
        self.effort.setEnabled(True)
        self.new_button.setEnabled(True)
        self.threads_button.setEnabled(True)
        self.choose_cwd.setEnabled(True)
        self.permission.setEnabled(True)
        self.login_button.setEnabled(not self.login_inflight)
        self.add_image.setEnabled(True)
        self.remove_image.setEnabled(bool(self.images))
        self._update_failed_drafts()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'hero'):
            self.hero.setVisible(self.height() >= 700)
        if hasattr(self, 'options_scroll'):
            self.options_scroll.setMaximumHeight(200 if self.height() < 700 else 250)

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
