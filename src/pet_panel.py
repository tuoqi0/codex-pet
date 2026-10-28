"""Shared Codex Pet conversation and usage window."""
import json
import math
from datetime import datetime
from pathlib import Path

from PyQt5 import sip
from PyQt5.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QFontDatabase, QPainter, QPen, QPixmap, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat, QTextOption
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog,
    QFrame, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from codex_backend import Backend, MAX_IMAGE_BYTES, MAX_IMAGE_COUNT, MAX_IMAGE_TOTAL_BYTES


def _same_directory(left, right):
    if not isinstance(left, str) or not left or not isinstance(right, str) or not right:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False


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


def _markdown_features():
    if not all(hasattr(QTextDocument, name) for name in
               ('setMarkdown', 'MarkdownNoHTML', 'MarkdownDialectGitHub', 'MarkdownFeatures')):
        return None
    return QTextDocument.MarkdownFeatures(QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML)


class _MessageDocument(QTextDocument):
    """Never load images, styles or other resources from generated content."""

    def loadResource(self, resource_type, url):
        return None


class MessageBubble(QTextEdit):
    """Selectable Markdown for assistants and literal text for the user."""

    def __init__(self, text, role):
        super().__init__()
        self.role = role
        self.raw_text = str(text)
        self.setObjectName('userBubble' if role == 'user' else 'assistantBubble')
        self.setReadOnly(True)
        self.setDocument(_MessageDocument(self))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(self._resize_to_document)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(50)
        self._render_timer.timeout.connect(self._render)
        self._render_pending = False
        self._pressed_link = None
        self.selectionChanged.connect(self._selection_changed)
        self._render()

    def mousePressEvent(self, event):
        self._pressed_link = None
        if event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier:
            href = self.anchorAt(event.pos())
            if href:
                self._pressed_link = (href, event.pos())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        pressed = self._pressed_link
        self._pressed_link = None
        super().mouseReleaseEvent(event)
        if (pressed is None or event.button() != Qt.LeftButton or event.modifiers() != Qt.NoModifier or
                (event.pos() - pressed[1]).manhattanLength() >= QApplication.startDragDistance() or
                self.textCursor().hasSelection() or self.anchorAt(event.pos()) != pressed[0]):
            return
        url = QUrl(pressed[0])
        if url.isValid() and url.scheme().lower() in ('http', 'https') and url.host():
            QDesktopServices.openUrl(url)

    def _resize_to_document(self, size=None):
        self.setFixedHeight(max(44, math.ceil(self.document().size().height()) + 24))

    def _render(self):
        # Re-rendering a document destroys selections. Defer until the user
        # finishes selecting or copying; incoming bytes remain in raw_text.
        if self.textCursor().hasSelection():
            self._render_pending = True
            return
        self._render_pending = False
        document = self.document()
        features = _markdown_features() if self.role != 'user' else None
        if features is not None:
            document.setMarkdown(self.raw_text, features)
            self._style_code(document)
        else:
            self.setPlainText(self.raw_text)

    def _style_code(self, document):
        # Qt marks code fragments fixed-pitch but inherits the UI font family;
        # select an actual fixed font and tint code to make it distinguishable.
        fonts = QFontDatabase()
        fixed_family = QFontDatabase.systemFont(QFontDatabase.FixedFont).family()
        if not fonts.isFixedPitch(fixed_family):
            fixed_family = next((name for name in ('DejaVu Sans Mono', 'Liberation Mono', 'Monospace')
                                 if name in fonts.families() and fonts.isFixedPitch(name)), fixed_family)
        spans = []
        blocks = []
        block = document.begin()
        while block.isValid():
            fenced = block.blockFormat().hasProperty(QTextFormat.BlockCodeFence)
            if fenced:
                blocks.append(block.position())
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.isValid() and (fenced or fragment.charFormat().fontFixedPitch()):
                    spans.append((fragment.position(), fragment.length(), not fenced))
                fragments += 1
            block = block.next()
        cursor = QTextCursor(document)
        for position in blocks:
            cursor.setPosition(position)
            block_format = cursor.blockFormat()
            block_format.setBackground(QColor('#e9e1f2'))
            cursor.setBlockFormat(block_format)
        for position, length, inline in spans:
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.KeepAnchor)
            char_format = QTextCharFormat()
            char_format.setFontFamily(fixed_family)
            if inline:
                char_format.setBackground(QColor('#e9e1f2'))
            cursor.mergeCharFormat(char_format)

    def _selection_changed(self):
        if self._render_pending and not self.textCursor().hasSelection():
            self._render_timer.start()

    def append_text(self, text):
        self.raw_text += str(text)
        # Reparse complete accumulated text, so delimiters split across deltas
        # form the same document as a complete response. Batch rapid deltas.
        if not self._render_timer.isActive():
            self._render_timer.start()



class ChatTranscript(QScrollArea):
    """Selectable messages with one continuously updated response."""

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
        self.scroll_restore_generation = 0
        self.pending_messages = None
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)
        self.verticalScrollBar().valueChanged.connect(self._position_changed)
        self.add_message('assistant', '你好呀，我在这里。想聊聊天，或一起处理什么事情？')

    def add_message(self, role, text):
        self._remember_scroll_position()
        group = QWidget()
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(5)
        name = QLabel('你' if role == 'user' else '✦  CODEX')
        name.setObjectName('bubbleName')
        bubble = MessageBubble(text, role)
        bubble.selectionChanged.connect(self._selection_changed)
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
        self.pending_messages = None
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
        return '\n'.join(bubble.raw_text for bubble in self.bubbles)

    def _selection_changed(self):
        if self.pending_messages is not None:
            # Run after the selection-change signal has finished; another refresh,
            # conversation switch or selection can supersede it first.
            QTimer.singleShot(0, self._apply_pending_messages)

    def _apply_pending_messages(self):
        if sip.isdeleted(self) or self.pending_messages is None or any(
                bubble.textCursor().hasSelection() for bubble in self.bubbles):
            return
        messages = self.pending_messages
        self.pending_messages = None
        self.replace_messages_preserving_scroll(messages)

    def replace_messages_preserving_scroll(self, messages):
        messages = list(messages)
        current = [('user' if bubble.objectName() == 'userBubble' else 'assistant', bubble.raw_text)
                   for bubble in self.bubbles]
        if current == messages:
            self.pending_messages = None
            return
        if messages[:len(current)] == current:
            # Appending leaves existing widgets and their active selections intact.
            self.pending_messages = None
            for role, text in messages[len(current):]:
                self.add_message(role, text)
            return
        if any(bubble.textCursor().hasSelection() for bubble in self.bubbles):
            # Keep only the latest complete snapshot until the user finishes selecting.
            self.pending_messages = messages
            return
        self.pending_messages = None
        bar = self.verticalScrollBar()
        old_value = bar.value()
        follow = old_value >= bar.maximum() - 8
        self.clear()
        generation = self.scroll_restore_generation
        self.follow_latest = False
        for role, text in messages:
            self.add_message(role, text)
            self.follow_latest = False
        self.messages.activate()
        def restore():
            if sip.isdeleted(self) or sip.isdeleted(bar):
                return
            if generation != self.scroll_restore_generation:
                return
            self.follow_latest = follow
            bar.setValue(bar.maximum() if follow else min(old_value, bar.maximum()))
            self.follow_latest = follow
        QTimer.singleShot(0, restore)

    def clear(self):
        self.pending_messages = None
        self.scroll_restore_generation += 1
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

    def safe_emit(self, event, data):
        # Polling workers may finish after Qt has destroyed their receiver.
        if sip.isdeleted(self):
            return
        try:
            self.received.emit(event, data)
        except RuntimeError:
            if not sip.isdeleted(self):
                raise


class PetPanel(QDialog):
    """One panel is shared by both pets; all widgets stay on the Qt thread."""

    limits_changed = pyqtSignal(str, object)

    def __init__(self, action_callback=None, parent=None, backend_factory=Backend):
        super().__init__(parent)
        self.setObjectName('petPanel')
        self.setWindowTitle('Codex Pet · 对话与额度')
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
        self.thread_refreshing = False
        self.thread_refresh_auto = False
        self.thread_follow_generation = 0
        self.thread_follow_request_generation = None
        self.thread_follow_failed = False
        self.images = []
        self.images_after_login_need_review = False
        self.inflight_draft = None
        self.failed_drafts = []
        self.progress_entries = []
        self.progress_plans = {}
        self.progress_mode = None
        self.bridge = _EventBridge(self)
        self.bridge.received.connect(self._event)
        self.backend = backend_factory(self.bridge.safe_emit)
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
        self.status.setText('✦  尚未连接。请先登录 ChatGPT。')
        self.status.setMaximumHeight(38)
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.thread_follow_timer = QTimer(self)
        self.thread_follow_timer.setInterval(30000)
        self.thread_follow_timer.timeout.connect(self._follow_thread_tick)
        self.thread_follow_timer.start()
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
        eyebrow = QLabel('✦  你的桌面伙伴')
        eyebrow.setObjectName('eyebrow')
        title = QLabel('Codex Pet')
        title.setObjectName('heroTitle')
        subtitle = QLabel('聊聊灵感，也陪你完成每一件事。')
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
        mode_label = QLabel('对话模式')
        mode_label.setObjectName('muted')
        row.addWidget(mode_label)
        self.mode = ModeSelector()
        for label, value in (('普通对话 · ChatGPT 计划', 'chat'),
                             ('计划 · ChatGPT 计划', 'plan'),
                             ('任务 · Codex', 'task')):
            self.mode.addItem(label, value)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        row.addWidget(self.mode, 1)
        self.login_button = QPushButton('登录 ChatGPT')
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
        self.effort_label = QLabel('推理强度')
        self.effort = ModeSelector()
        self.effort.currentIndexChanged.connect(self._effort_changed)
        model_options.addWidget(self.effort_label)
        model_options.addWidget(self.effort, 1)
        self.new_button = QPushButton('新对话')
        self.new_button.setObjectName('compact')
        self.new_button.clicked.connect(self._new_conversation)
        model_options.addWidget(self.new_button)
        self.threads_button = QPushButton('选择 Codex 对话')
        self.threads_button.setObjectName('compact')
        self.threads_button.clicked.connect(self._open_threads)
        model_options.addWidget(self.threads_button)
        self.thread_refresh = QPushButton('刷新已选对话')
        self.thread_refresh.setObjectName('compact')
        self.thread_refresh.setToolTip('只重新读取已选 Codex 对话；不发送消息或执行任务。')
        self.thread_refresh.clicked.connect(self._refresh_thread)
        model_options.addWidget(self.thread_refresh)
        card_layout.addLayout(model_options)
        self.thread_follow = QCheckBox('自动跟随已选对话（约 30 秒，非即时）')
        self.thread_follow.setChecked(True)
        self.thread_follow.setToolTip('只在此面板显示且任务模式空闲时读取已选对话，可随时关闭。')
        self.thread_follow.toggled.connect(self._follow_toggled)
        card_layout.addWidget(self.thread_follow)
        self.notice = QLabel()
        self.notice.setObjectName('notice')
        self.notice.setWordWrap(True)
        card_layout.addWidget(self.notice)
        project = QHBoxLayout()
        self.project_label = QLabel('工作目录：尚未选择')
        self.project_label.setObjectName('muted')
        self.project_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.project_label.setWordWrap(True)
        project.addWidget(self.project_label, 1)
        self.choose_cwd = QPushButton('选择目录')
        self.choose_cwd.setObjectName('compact')
        self.choose_cwd.clicked.connect(self._choose_cwd)
        project.addWidget(self.choose_cwd)
        card_layout.addLayout(project)
        permission = QHBoxLayout()
        self.permission_label = QLabel('本次权限')
        self.permission = ModeSelector()
        self.permission.addItem('请选择本次权限', None)
        self.permission.addItem('只读', 'read-only')
        self.permission.addItem('工作目录可写', 'workspace-write')
        self.permission.setToolTip('只作用于本次桌宠执行；不代表继承原对话权限。需要时会请求审批。')
        permission.addWidget(self.permission_label)
        permission.addWidget(self.permission, 1)
        card_layout.addLayout(permission)
        self.permission_notice = QLabel('仅适用本次执行，不代表继承原对话权限；需要时请求审批。')
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
        self.progress_toggle = QPushButton('执行进度 ▾')
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
        self.prompt.setPlaceholderText('写下想说的话，或者描述一个任务…')
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
        self.attachment_review = QLabel('登录已更新；附件仍在草稿中。发送前请确认账户和图片。')
        self.attachment_review.setObjectName('notice')
        self.attachment_review.setWordWrap(True)
        self.attachment_review.hide()
        attachment_content.addWidget(self.attachment_review)
        self.image_list = QListWidget()
        self.image_list.setObjectName('imageList')
        self.image_list.setMaximumHeight(54)
        self.image_list.setAccessibleName('本次发送的图片')
        attachment_content.addWidget(self.image_list)
        attachment_row.addLayout(attachment_content, 1)
        self.remove_image = QPushButton('移除所选')
        self.remove_image.setObjectName('compact')
        self.remove_image.clicked.connect(self._remove_image)
        attachment_row.addWidget(self.remove_image)
        self.attachments.hide()
        layout.addWidget(self.attachments)
        buttons = QHBoxLayout()
        self.add_image = QPushButton('＋ 图片')
        self.add_image.setObjectName('compact')
        self.add_image.clicked.connect(self._choose_images)
        buttons.addWidget(self.add_image)
        self.restore_draft = QPushButton('恢复失败草稿')
        self.restore_draft.setObjectName('compact')
        self.restore_draft.setToolTip('把先前失败的内容加回目前草稿，不会覆盖目前输入。')
        self.restore_draft.clicked.connect(self._restore_failed_draft)
        self.restore_draft.hide()
        buttons.addWidget(self.restore_draft)
        buttons.addStretch(1)
        self.cancel_button = QPushButton('停止')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.backend_cancel)
        self.send_button = QPushButton('发送  ↗')
        self.send_button.setObjectName('primary')
        self.send_button.clicked.connect(self._send)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)
        self.tabs.addTab(page, '对话与任务')
        self._mode_changed()

    def _build_limits(self):
        page = QWidget()
        page.setObjectName('limitsContent')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)
        heading = QLabel('账户与用量')
        heading.setObjectName('sectionTitle')
        layout.addWidget(heading)
        description = QLabel('查看目前授权账户及 Codex 回传的用量窗口。')
        description.setObjectName('muted')
        description.setWordWrap(True)
        layout.addWidget(description)
        account = self._card()
        account_layout = QVBoxLayout(account)
        account_layout.setContentsMargins(17, 16, 17, 16)
        account_layout.setSpacing(12)
        account_heading = QLabel('✦  已连接的账户')
        account_heading.setObjectName('sectionTitle')
        account_layout.addWidget(account_heading)
        self.account_text = QLabel('ChatGPT 授权：尚未查询')
        self.account_text.setWordWrap(True)
        self.codex_account_text = QLabel('Codex 账户：尚未查询')
        self.codex_account_text.setWordWrap(True)
        account_layout.addWidget(self.account_text)
        account_layout.addWidget(self.codex_account_text)
        layout.addWidget(account)
        usage = self._card()
        usage_layout = QVBoxLayout(usage)
        usage_layout.setContentsMargins(17, 16, 17, 16)
        usage_layout.setSpacing(12)
        usage_heading = QLabel('◷  Codex 额度')
        usage_heading.setObjectName('sectionTitle')
        usage_layout.addWidget(usage_heading)
        self.limit_text = QLabel('Codex 额度：尚未查询')
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
        refresh = QPushButton('刷新额度')
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
        self.tabs.addTab(scroll, '账户与额度')

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
        self.status.setText('正在启动 ChatGPT 授权…')
        self.backend.login()

    def refresh_limits(self, include_account=True):
        if self.limits_inflight:
            return False
        self.limits_inflight = True
        self.limits_changed.emit('loading', None)
        self.action_callback('quota')
        self.status.setText('正在查询 Codex 额度…')
        if include_account:
            self.backend.account()
        self.backend.limits()
        return True

    def backend_cancel(self):
        if self.busy:
            self.cancel_button.setEnabled(False)
            self.status.setText('正在请求停止，等待 Codex 确认结果…')
        self.backend.cancel()

    def _mode_changed(self):
        mode = self.mode.currentData()
        if mode != 'task' and hasattr(self, 'thread_follow_generation'):
            self.thread_follow_generation += 1
        if self.busy and mode != self.active_mode:
            self.mode.blockSignals(True)
            self.mode.setCurrentIndex(self.mode.findData(self.active_mode))
            self.mode.blockSignals(False)
            self.status.setText('正在执行，请先停止或等待完成再切换模式。')
            return
        notices = {
            'chat': '普通对话使用 ChatGPT 授权，无本地工具。图片加入上下文后，后续请求会再次传送，直至新对话。',
            'plan': '计划使用 ChatGPT 授权及只读工作目录；用量可能与 Codex 共享。',
            'task': '任务使用已登录的 Codex ChatGPT 账户；可依设定执行工具与请求审批。',
        }
        self.notice.setText(notices[mode] + ' 各模式各自保留上下文；不会同步 ChatGPT 网站对话。')
        image_notice = ('普通对话：发送的图片会随后续请求再次传送，直至新对话。'
                        if mode == 'chat' else
                        '计划／任务：图片由 Codex 线程管理；临时副本在执行结束后清理。')
        self.upload_hint.setText(image_notice)
        self.add_image.setToolTip(
            '选择本机图片，按发送时上传。' + image_notice +
            f'最多 {MAX_IMAGE_COUNT} 张，单张 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB，'
            f'总计 {MAX_IMAGE_TOTAL_BYTES // (1024 * 1024)} MiB。')
        if hasattr(self, 'transcripts'):
            if mode != 'task':
                self.transcripts['task'].pending_messages = None
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
            self.status.setText('正在查询可用模型…')
        self.backend.models(mode, self.model_generation[mode])

    def _show_models(self, mode):
        self.model.blockSignals(True)
        self.model.clear()
        entries = self.model_cache.get(mode, [])
        if mode == 'task' and self.selected_thread:
            self.model.addItem('沿用所选对话的模型与强度', {'id': None, 'follow_thread': True})
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
            self.model.addItem('尚无模型，请刷新', None)
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
        self.effort.addItem('默认' + ('（' + default + '）' if default else ''), None)
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
        self.thread_refresh.setVisible(visible)
        self.thread_refresh.setEnabled(visible and not self.busy and not self.thread_loading)
        self.thread_follow.setVisible(visible)

    def _new_conversation(self):
        if self.busy or self.thread_loading:
            return
        mode = self.mode.currentData()
        self.backend.new_thread(mode, self.cwd)
        self.transcript.clear()
        self.transcript.add_message('assistant', '新的对话已开始。')
        if mode == 'task':
            self.selected_thread = None
            if self.model_choice.get('task') == '__thread__':
                self.model_choice.pop('task')
            self.permission.setCurrentIndex(0)
            self._show_permission()
            self._show_models(mode)
        self.status.setText('已开始新对话；原有 Codex 历史不会删除。')

    def _choose_cwd(self):
        if self.busy or self.thread_loading:
            return
        selected = QFileDialog.getExistingDirectory(self, '选择工作目录', self.cwd or str(Path.home()))
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
            self, '选择要随本次对话上传的图片', str(Path.home()),
            '图片 (*.png *.PNG *.jpg *.JPG *.jpeg *.JPEG *.webp *.WEBP *.gif *.GIF)')
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
            self.status.setText(f'一次最多附加 {MAX_IMAGE_COUNT} 张图片，请先移除不需要的图片。')
        elif added:
            self.status.setText('图片已选择；按发送时上传。' + self.upload_hint.text())

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
            self.restore_draft.setText(f'恢复失败草稿 ({len(self.failed_drafts)})')
        self.restore_draft.setEnabled(not self.busy)

    def _restore_failed_draft(self):
        if self.busy or not self.failed_drafts:
            return
        draft = self.failed_drafts[0]
        if (draft['mode'] != self.mode.currentData()
                or (draft['mode'] != 'chat' and draft['cwd'] != self.cwd)):
            self.status.setText('请切回原对话模式和工作目录，再恢复这份草稿。')
            return
        images = self.images + [path for path in draft['images'] if path not in self.images]
        if len(images) > MAX_IMAGE_COUNT:
            self.status.setText('图片数量超过上限，请先移除目前草稿中的图片，再恢复。')
            return
        current = self.prompt.toPlainText()
        self.prompt.setPlainText(current + ('\n\n' if current else '') + draft['text'])
        self.images = images
        self.images_after_login_need_review = (self.images_after_login_need_review
                                                or bool(draft.get('review_after_login') and draft['images']))
        self.failed_drafts.pop(0)
        self._show_images()
        self._update_failed_drafts()
        self.status.setText('已恢复草稿。上次请求可能已部分处理，重试可能重复。')

    def _open_threads(self):
        if self.busy or self.thread_dialog is not None:
            return
        if self.thread_loading:
            self.status.setText('上一个对话查询仍在进行，请稍候。')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('选择 Codex 对话')
        dialog.setModal(True)
        dialog.resize(560, 420)
        layout = QVBoxLayout(dialog)
        note = QLabel('仅在你选择后读取完整对话；继续任务会使用该对话的工作目录。'
                      '若其他 Codex 窗口同时操作同一对话，可能造成冲突。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.thread_list = QListWidget(dialog)
        layout.addWidget(self.thread_list, 1)
        self.thread_status = QLabel('正在载入…')
        self.thread_status.setWordWrap(True)
        layout.addWidget(self.thread_status)
        row = QHBoxLayout()
        self.thread_more = QPushButton('载入更多')
        self.thread_more.clicked.connect(lambda: self._load_threads(self.thread_cursor))
        self.thread_open = QPushButton('读取所选对话')
        self.thread_open.clicked.connect(self._read_thread)
        close = QPushButton('关闭')
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
        self.thread_more.setText('载入更多')
        self.thread_more.setEnabled(False)
        self.thread_open.setEnabled(False)
        self.thread_status.setText('正在载入对话列表…')
        self.backend.codex_threads(cursor)

    def _read_thread(self):
        item = self.thread_list.currentItem()
        if self.thread_loading or item is None:
            return
        self.thread_reading = item.data(Qt.UserRole)
        self.thread_loading = True
        self.thread_more.setEnabled(False)
        self.thread_open.setEnabled(False)
        self.thread_status.setText('正在读取所选对话…')
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
            item = QListWidgetItem(str(title)[:160] + '\n' + str(thread.get('cwd') or '工作目录不可用'))
            item.setData(Qt.UserRole, thread['id'])
            self.thread_list.addItem(item)
        self.thread_cursor = result.get('nextCursor')
        self.thread_loading = False
        self.thread_more.setEnabled(bool(self.thread_cursor))
        self.thread_open.setEnabled(True)
        self.thread_status.setText('请选择对话后点击读取。' if self.thread_list.count() else '没有找到 Codex 对话。')

    def _follow_toggled(self, checked):
        self.thread_follow_generation += 1
        self.thread_follow_failed = False

    def hideEvent(self, event):
        self.thread_follow_generation += 1
        super().hideEvent(event)

    def _follow_thread_tick(self):
        if not self.isVisible() or not self.thread_follow.isChecked():
            return
        self._start_thread_refresh(automatic=True)

    def _refresh_thread(self):
        self._start_thread_refresh(automatic=False)

    def _start_thread_refresh(self, automatic):
        if (self.busy or self.thread_loading or self.thread_dialog is not None or
                not self.selected_thread or self.mode.currentData() != 'task'):
            return
        self.thread_reading = self.selected_thread
        self.thread_loading = True
        self.thread_refreshing = True
        self.thread_refresh_auto = automatic
        self.thread_follow_request_generation = self.thread_follow_generation if automatic else None
        self._show_permission()
        self.send_button.setEnabled(False)
        self.send_button.setToolTip('正在读取已选对话，完成后可发送。')
        self.new_button.setEnabled(False)
        self.threads_button.setEnabled(False)
        self.choose_cwd.setEnabled(False)
        if not automatic:
            self.status.setText('正在刷新已选对话…')
        self.backend.codex_thread(self.thread_reading)

    def _follow_result_is_stale(self):
        return (self.thread_refresh_auto and
                (not self.isVisible() or not self.thread_follow.isChecked() or
                 self.mode.currentData() != 'task' or
                 self.thread_follow_request_generation != self.thread_follow_generation or
                 self.selected_thread != self.thread_reading))

    def _finish_thread_refresh(self):
        self.thread_loading = False
        self.thread_reading = None
        self.thread_refreshing = False
        self.thread_refresh_auto = False
        self.thread_follow_request_generation = None
        self._show_permission()
        if not self.busy:
            self.send_button.setEnabled(True)
            self.new_button.setEnabled(True)
            self.threads_button.setEnabled(True)
            self.choose_cwd.setEnabled(True)
        self.send_button.setToolTip('')

    def _show_thread(self, result):
        if not self.thread_loading or not self.thread_reading:
            return
        refreshing = self.thread_refreshing
        automatic = self.thread_refresh_auto
        if refreshing and self._follow_result_is_stale():
            self._finish_thread_refresh()
            return
        if self.thread_dialog is None and not refreshing:
            self.thread_loading = False
            self.thread_reading = None
            return
        thread = result.get('thread') if isinstance(result, dict) else None
        invalid = not isinstance(thread, dict)
        if not invalid:
            turns = thread.get('turns')
            invalid = (thread.get('id') != self.thread_reading or not isinstance(thread.get('cwd'), str) or not thread.get('cwd') or
                       (refreshing and (self.selected_thread != self.thread_reading or not _same_directory(self.cwd, thread['cwd']))) or
                       not isinstance(turns, list))
        if not invalid:
            for turn in turns:
                if not isinstance(turn, dict) or not isinstance(turn.get('items'), list):
                    invalid = True
                    break
                for item in turn['items']:
                    if not isinstance(item, dict) or (item.get('type') == 'userMessage' and
                                                     not isinstance(item.get('content'), list)) or (
                            item.get('type') == 'fileChange' and item.get('changes') is not None and
                            not isinstance(item.get('changes'), list)):
                        invalid = True
                        break
                if invalid:
                    break
        if invalid:
            if refreshing:
                self._finish_thread_refresh()
                if not automatic or not self.thread_follow_failed:
                    self.status.setText('无法确认已选对话或工作目录，刷新失败。')
                if automatic:
                    self.thread_follow_failed = True
            else:
                self.thread_loading = False
                self.thread_reading = None
                self.thread_open.setEnabled(True)
                self.thread_status.setText('无法确认对话或工作目录，请重试。')
            return
        messages = []
        for turn in turns:
            for item in turn.get('items', []):
                kind = item.get('type')
                if kind == 'userMessage':
                    parts = []
                    for part in item.get('content', []):
                        if not isinstance(part, dict):
                            continue
                        if part.get('type') in ('image', 'localImage', 'input_image'):
                            parts.append('[图片]')
                        elif part.get('text'):
                            parts.append(str(part['text']))
                    text = '\n'.join(part for part in parts if part)
                    if text:
                        messages.append(('user', text))
                elif kind == 'agentMessage' and item.get('text'):
                    messages.append(('assistant', str(item['text'])))
                elif kind == 'plan' and item.get('text'):
                    messages.append(('assistant', '[历史计划]\n' + str(item['text'])))
                elif kind in ('commandExecution', 'fileChange', 'webSearch',
                              'mcpToolCall', 'dynamicToolCall', 'contextCompaction'):
                    labels = {'commandExecution': '命令', 'fileChange': '文件变更',
                              'webSearch': '网络搜索', 'mcpToolCall': 'MCP 工具',
                              'dynamicToolCall': '工具', 'contextCompaction': '上下文整理'}
                    summary = '[历史执行] ' + labels[kind]
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
                    messages.append(('assistant', summary))
        if not _same_directory(self.cwd, thread['cwd']):
            self.backend.new_thread('plan', thread['cwd'])
            self.transcripts['plan'].clear()
        transcript = self.transcripts['task']
        if refreshing:
            transcript.replace_messages_preserving_scroll(messages)
            self._finish_thread_refresh()
            if not automatic:
                self.status.setText('已刷新所选 Codex 对话；草稿和本次权限保持不变。')
            elif self.thread_follow_failed:
                self.thread_follow_failed = False
                self.status.setText('已恢复跟随所选 Codex 对话。')
            return
        transcript.clear()
        for role, text in messages:
            transcript.add_message(role, text)
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
        self.status.setText('已读取 Codex 对话；发送后会在此对话中继续。')

    def _send(self):
        draft_text = self.prompt.toPlainText()
        text = draft_text.strip()
        mode = self.mode.currentData()
        if (not text and not self.images) or self.busy or self.thread_loading:
            return
        if self.login_inflight:
            self.status.setText('正在等待授权完成，请稍后再发送。')
            return
        if mode != 'chat' and not self.cwd:
            QMessageBox.information(self, '选择工作目录', '计划和任务需要先选择工作目录。')
            return
        selected = self.model.currentData()
        if not selected:
            self.status.setText('没有可选模型，请先刷新模型。')
            return
        if mode == 'task' and self.selected_thread and not self.permission.currentData():
            self.status.setText('请先选择本次执行权限；不会自动沿用原对话权限。')
            self.options_scroll.ensureWidgetVisible(self.permission)
            self.permission.setFocus()
            return
        if self.images and self.images_after_login_need_review:
            names = '\n'.join(Path(path).name for path in self.images)
            confirmation = QMessageBox.question(
                self, '确认附件与登录账户',
                '登录已更新，请确认目前模式使用的账户，并确认要发送这些草稿图片：\n\n' + names,
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
        self.thread_refresh.setEnabled(False)
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
        self.inflight_draft = {'mode': mode, 'cwd': self.cwd, 'text': draft_text, 'images': images,
                               'selected_thread': self.selected_thread if mode == 'task' else None}
        draft = self.inflight_draft
        self.images.clear()
        self._show_images()
        names = '\n'.join(Path(path).name for path in images)
        self.transcript.start_exchange(text + ('\n\n[图片附件]\n' + names if names else ''))
        if mode in ('task', 'plan'):
            self.action_callback('work')
        request_id = self.backend.send(mode, text, self.cwd, selected['id'],
                                       self.effort.currentData() if mode == 'task' else None,
                                       self.selected_thread if mode == 'task' else None,
                                       self.permission.currentData() if mode == 'task' and self.selected_thread else None,
                                       images or None)
        if self.inflight_draft is draft:
            if request_id:
                draft['request_id'] = request_id
            else:
                # Backend.send reports synchronous rejection with an error but
                # cannot emit done because no worker was started. Reuse the
                # ordinary failure path to restore the draft and UI controls.
                self._event('done', {'mode': mode, 'status': 'failed'})

    def _append(self, text):
        self.transcript.append_response(text)

    def _toggle_progress(self):
        self.progress_log.setVisible(self.progress_log.isHidden())
        self.progress_toggle.setText('执行进度 ▾' if not self.progress_log.isHidden() else '执行进度 ▸')

    def _progress(self, data):
        if not isinstance(data, dict):
            return
        kind = data.get('kind')
        if kind == 'item':
            labels = {'commandExecution': '命令', 'fileChange': '文件变更', 'webSearch': '网络搜索',
                      'mcpToolCall': 'MCP 工具', 'dynamicToolCall': '工具', 'contextCompaction': '上下文整理'}
            phase = '开始' if data.get('phase') == 'started' else '结束'
            line = f'{phase}：{labels.get(data.get("type"), "工具")}'
            if data.get('status'):
                line += ' · ' + str(data['status'])
            if data.get('detail'):
                line += '\n' + str(data['detail'])[:4000]
            self.progress_entries.append(line)
            self.progress_entries = self.progress_entries[-200:]
        elif kind == 'model_rerouted':
            line = f'模型已变更：{data.get("from_model") or "未知"} → {data.get("to_model") or "未知"}'
            if data.get('reason'):
                line += '\n原因：' + str(data['reason'])[:4000]
            self.progress_entries.append(line)
            self.progress_entries = self.progress_entries[-200:]
        elif kind == 'plan':
            steps = data.get('steps') or []
            lines = ['执行计划：']
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
        if event == 'thread':
            draft = self.inflight_draft
            thread = data.get('thread') if isinstance(data, dict) else None
            if (not isinstance(data, dict) or not self.busy or self.active_mode != 'task' or not draft or draft['mode'] != 'task'
                    or data.get('mode') != 'task' or not draft.get('request_id')
                    or data.get('request_id') != draft['request_id'] or not isinstance(thread, dict)):
                return
            ident, directory = thread.get('id'), thread.get('cwd')
            previous = draft.get('selected_thread')
            if (not isinstance(ident, str) or not ident or not isinstance(directory, str) or not directory
                    or self.selected_thread != previous or (previous and previous != ident)
                    or not isinstance(self.cwd, str) or not isinstance(draft['cwd'], str)):
                return
            try:
                if (Path(directory).expanduser().resolve() != Path(draft['cwd']).expanduser().resolve()
                        or Path(self.cwd).expanduser().resolve() != Path(draft['cwd']).expanduser().resolve()):
                    return
            except (OSError, RuntimeError, ValueError):
                return
            self.selected_thread = ident
            self._show_permission()
        elif event == 'models':
            mode = data.get('mode')
            if mode not in self.model_generation or data.get('generation') != self.model_generation[mode]:
                return
            self.models_loading.discard(mode)
            entries = [item for item in data.get('models', []) if isinstance(item, dict) and item.get('id')]
            self.model_cache[mode] = entries
            if mode == self.mode.currentData():
                self._show_models(mode)
                if not self.busy:
                    self.status.setText('已更新可用模型。' if entries else '没有可选模型，请刷新。')
        elif event == 'models_error':
            mode = data.get('mode')
            if mode not in self.model_generation or data.get('generation') != self.model_generation[mode]:
                return
            self.models_loading.discard(mode)
            if mode == self.mode.currentData():
                self._show_models(mode)
                self.status.setText('模型查询失败：' + str(data.get('message', '未知错误')))
        elif event == 'codex_threads':
            self._show_thread_list(data)
        elif event == 'codex_thread':
            self._show_thread(data)
        elif event in ('codex_threads_error', 'codex_thread_error'):
            automatic = self.thread_refresh_auto
            stale = self._follow_result_is_stale()
            self._finish_thread_refresh()
            if stale:
                return
            if automatic:
                if not self.thread_follow_failed:
                    self.status.setText('自动跟随暂不可用；可手动刷新。')
                self.thread_follow_failed = True
                return
            if self.thread_dialog is not None:
                self.thread_status.setText('对话查询失败：' + str(data))
                self.thread_more.setText('重试')
                self.thread_more.setEnabled(True)
                self.thread_open.setEnabled(True)
            else:
                self.status.setText('对话查询失败：' + str(data))
        elif event == 'delta':
            self._append(data)
        elif event == 'progress':
            self._progress(data)
        elif event == 'status':
            self.status.setText(str(data))
        elif event == 'error':
            self.status.setText(str(data))
            if self.busy:
                self._append('\n[错误] ' + str(data) + '\n')
                self.last_error = str(data)
            else:
                self.transcript.add_message('assistant', '[错误] ' + str(data))
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
                self.transcripts[mode].add_message('assistant', '授权已更新，已开始新的对话。')
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
                message = '任务结果未知，无法确认是否已停止或完成。请先检查 Codex 对话及工作目录；重试可能重复执行。'
                self.status.setText(message)
                self.transcript.add_message('assistant', message)
            elif outcome == 'interrupted':
                self.status.setText('已确认停止；此前可能已有部分操作。')
            elif self.last_error:
                self.status.setText('请求失败：' + self.last_error)
            else:
                self.status.setText('已结束：' + str(outcome or '未知'))
            draft = self.inflight_draft
            self.inflight_draft = None
            if outcome != 'completed' and draft:
                if not self.prompt.toPlainText() and not self.images:
                    self.prompt.setPlainText(draft['text'])
                    self.images = draft['images']
                    self._show_images()
                    self.status.setText(self.status.text() + '；草稿已恢复，上次请求可能已部分处理。')
                else:
                    self.failed_drafts.append(draft)
                    self.status.setText(self.status.text() + '；草稿可恢复，上次请求可能已部分处理。')
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
            label = 'Codex 账户：' if target is self.codex_account_text else 'ChatGPT 授权：'
            target.setText(label + str(info.get('email') or info.get('type') or '不可用') +
                           '　方案：' + str(info.get('planType') or '不可用'))
        elif event == 'limits':
            self.limits_inflight = False
            self._show_limits(data)
            self.limits_changed.emit('limits', data)
        elif event == 'limits_error':
            self.limits_inflight = False
            self.limit_text.setText('Codex 额度查询失败：' + str(data))
            for widget, _, _ in self.usage_bars:
                widget.hide()
            self.status.setText('额度查询失败：' + str(data))
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
        self._show_permission()
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
                lines.append(f'{name} {label}：已用 {used if used is not None else "不可用"}%　窗口 {minutes if minutes is not None else "不可用"} 分钟　重置 {reset_text}')
                if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) and 0 <= used <= 100:
                    metrics.append((f'{name} · {label}  {used}%', round(used)))
        self.limit_text.setText('\n'.join(lines) if lines else 'Codex 额度：不可用')
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
                options = [option for option in (question.get('options') or [])
                           if isinstance(option, dict) and isinstance(option.get('label'), str)]
                labels = [option['label'] for option in options]
                prompt = str(question.get('question', ''))
                if options:
                    descriptions = [option['label'] + (('：' + str(option['description']))
                                    if option.get('description') else '') for option in options]
                    prompt += '\n\n' + '\n'.join(descriptions)
                secret = bool(question.get('isSecret'))
                allow_other = bool(question.get('isOther'))
                while True:
                    if labels and not secret:
                        value, ok = QInputDialog.getItem(self, 'Codex 需要你的回答', prompt,
                                                         labels, 0, allow_other)
                    else:
                        echo = QLineEdit.Password if secret else QLineEdit.Normal
                        value, ok = QInputDialog.getText(self, 'Codex 需要你的回答', prompt, echo)
                    if not ok:
                        self.backend.approve(data['id'], 'cancel')
                        return
                    if not labels or allow_other or value in labels:
                        break
                    prompt = str(question.get('question', '')) + '\n请输入列出的选项。\n' + '\n'.join(labels)
                answers[str(question.get('id', ''))] = {'answers': [str(value)]}
            self.backend.approve(data['id'], {'answers': answers})
            return
        if method not in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                          'item/permissions/requestApproval'):
            self.backend.approve(data['id'], 'decline')
            self.status.setText('不支持的审批请求已拒绝：' + method)
            return
        params = data.get('params', {})
        allowed = params.get('availableDecisions') if method == 'item/commandExecution/requestApproval' else None
        choices = (('accept', 'decline') if method == 'item/permissions/requestApproval'
                   else ('accept', 'decline', 'cancel')) if allowed is None else tuple(
            decision for decision in ('accept', 'decline', 'cancel')
            if isinstance(allowed, list) and decision in allowed)
        if not choices:
            self.backend.approve(data['id'], 'unsupported')
            self.status.setText('此审批的决定选项暂不支持，已拒绝处理。')
            return
        details = json.dumps(params, ensure_ascii=False, indent=2, default=str)
        if method == 'item/fileChange/requestApproval':
            changes = data.get('file_changes')
            if changes:
                details += '\n\n文件变更详情（来自同一会话、轮次和项目）：\n'
                for change in changes:
                    details += '\n' + str(change['path']) + '  ' + json.dumps(change['kind'], ensure_ascii=False) + '\n' + change['diff'] + '\n'
            else:
                details += '\n\n文件差异不可用：未收到匹配的完整变更，或变更超过显示限制。请先在 Codex 中检查。'
        dialog = QDialog(self)
        dialog.setWindowTitle('Codex 请求审批')
        dialog.resize(600, 400)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('请检查请求后决定：'))
        body = QPlainTextEdit(dialog)
        body.setReadOnly(True)
        body.setPlainText(details)
        layout.addWidget(body)
        buttons = QDialogButtonBox(parent=dialog)
        selected = []
        names = {'accept': '允许', 'decline': '拒绝', 'cancel': '拒绝并停止'}
        for choice in choices:
            button = buttons.addButton(names[choice], QDialogButtonBox.ActionRole)
            if choice == ('decline' if 'decline' in choices else 'cancel'):
                button.setDefault(True)
            button.clicked.connect(lambda checked=False, value=choice: (selected.append(value), dialog.accept()))
        layout.addWidget(buttons)
        dialog.exec_()
        decision = selected[0] if selected else next((value for value in ('decline', 'cancel') if value in choices), 'unsupported')
        self.backend.approve(data['id'], decision)

    def closeEvent(self, event):
        self.hide()
        event.ignore()
