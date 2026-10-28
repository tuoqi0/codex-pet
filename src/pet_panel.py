"""Shared Codex Pet conversation and usage window."""
import json
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget,
    QVBoxLayout, QWidget,
)

from codex_backend import Backend


class _EventBridge(QObject):
    received = pyqtSignal(str, object)


class PetPanel(QDialog):
    """One panel is shared by both pets; all widgets stay on the Qt thread."""

    def __init__(self, action_callback=None, parent=None, backend_factory=Backend):
        super().__init__(parent)
        self.setWindowTitle('Codex Pet · 對話與額度')
        self.resize(590, 620)
        self.action_callback = action_callback or (lambda action: None)
        self.cwd = None
        self.busy = False
        self.last_error = None
        self.bridge = _EventBridge(self)
        self.bridge.received.connect(self._event)
        self.backend = backend_factory(self.bridge.received.emit)
        self.tabs = QTabWidget(self)
        self._build_chat()
        self._build_limits()
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        self.status = QLabel('尚未連接。請先登入 ChatGPT。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.backend.close)

    def _build_chat(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel('模式'))
        self.mode = QComboBox()
        for label, value in (('普通對話 · ChatGPT 計劃', 'chat'),
                             ('計劃 · ChatGPT 計劃', 'plan'),
                             ('任務 · Codex', 'task')):
            self.mode.addItem(label, value)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        row.addWidget(self.mode, 1)
        self.login_button = QPushButton('登入 ChatGPT')
        self.login_button.clicked.connect(self.backend_login)
        row.addWidget(self.login_button)
        layout.addLayout(row)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        project = QHBoxLayout()
        self.project_label = QLabel('尚未選擇工作目錄')
        self.project_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.project_label.setWordWrap(True)
        project.addWidget(self.project_label, 1)
        choose = QPushButton('選擇目錄…')
        choose.clicked.connect(self._choose_cwd)
        project.addWidget(choose)
        layout.addLayout(project)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText('在下方輸入訊息。')
        layout.addWidget(self.transcript, 1)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText('輸入訊息或描述任務')
        self.prompt.setMaximumHeight(120)
        layout.addWidget(self.prompt)
        buttons = QHBoxLayout()
        self.send_button = QPushButton('發送')
        self.send_button.clicked.connect(self._send)
        self.cancel_button = QPushButton('停止')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.backend_cancel)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)
        self.tabs.addTab(page, '對話與任務')
        self._mode_changed()

    def _build_limits(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self.account_text = QLabel('ChatGPT 授權：尚未查詢')
        self.account_text.setWordWrap(True)
        self.codex_account_text = QLabel('Codex 帳戶：尚未查詢')
        self.codex_account_text.setWordWrap(True)
        self.limit_text = QLabel('Codex 額度：尚未查詢')
        self.limit_text.setWordWrap(True)
        self.limit_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.account_text)
        layout.addWidget(self.codex_account_text)
        layout.addWidget(self.limit_text)
        layout.addStretch(1)
        row = QHBoxLayout()
        refresh = QPushButton('刷新額度')
        refresh.clicked.connect(self.refresh_limits)
        manage = QPushButton('管理 ChatGPT 用量')
        manage.clicked.connect(lambda: QDesktopServices.openUrl(QUrl('https://chatgpt.com/#settings/Usage')))
        row.addWidget(refresh)
        row.addWidget(manage)
        layout.addLayout(row)
        self.tabs.addTab(page, '帳戶與額度')

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

    def refresh_limits(self):
        self.action_callback('quota')
        self.status.setText('正在查詢 Codex 額度…')
        self.backend.account()
        self.backend.limits()

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
        self._append('\n你：' + text + '\n\nCodex：')
        if mode in ('task', 'plan'):
            self.action_callback('work')
        self.backend.send(mode, text, self.cwd)

    def _append(self, text):
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(str(text))
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    def _event(self, event, data):
        if event == 'delta':
            self._append(data)
        elif event == 'status':
            self.status.setText(str(data))
        elif event == 'error':
            self.status.setText(str(data))
            self._append('\n[錯誤] ' + str(data) + '\n')
            if self.busy:
                self.last_error = str(data)
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
            self._show_limits(data)
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
        self.limit_text.setText('\n'.join(lines) if lines else 'Codex 額度：不可用')

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
