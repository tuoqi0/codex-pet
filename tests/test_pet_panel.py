import os
import sys
import tempfile
import shutil
import threading
from unittest import mock
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PyQt5.QtCore import Qt, QTimer
from PyQt5 import sip
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton
import pet_panel
from pet_panel import PetPanel


class FakeBackend:
    def __init__(self, emit):
        self.emit = emit
        self.calls = []
        self.request_count = 0

    def login(self): self.calls.append(('login',))
    def account(self): self.calls.append(('account',))
    def limits(self): self.calls.append(('limits',))
    def send(self, *args):
        self.calls.append(('send', *args))
        self.request_count += 1
        return str(self.request_count)
    def models(self, mode, generation=None): self.calls.append(('models', mode, generation))
    def codex_threads(self, cursor=None): self.calls.append(('codex_threads', cursor))
    def codex_thread(self, thread_id): self.calls.append(('codex_thread', thread_id))
    def new_thread(self, mode, cwd): self.calls.append(('new_thread', mode, cwd))
    def cancel(self): self.calls.append(('cancel',))
    def approve(self, *args): self.calls.append(('approve', *args))
    def close(self): self.calls.append(('close',))


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.actions = []
        self.panel = PetPanel(action_callback=self.actions.append, backend_factory=FakeBackend)
        self.panel.model_cache = {mode: [{'id': 'model', 'name': 'Model', 'efforts': ['low', 'high'],
                                         'default_effort': 'high'}] for mode in ('chat', 'plan', 'task')}
        self.panel._show_models('chat')

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_question_choices_and_secret_are_respected(self):
        question = {'id': 'q', 'question': '選擇', 'isSecret': False, 'isOther': False,
                    'options': [{'label': 'A', 'description': '描述 A'}, {'label': 'B', 'description': '描述 B'}]}
        with mock.patch.object(pet_panel.QInputDialog, 'getItem', return_value=('B', True)) as choose:
            self.panel._approval({'id': 1, 'method': 'item/tool/requestUserInput', 'params': {'questions': [question]}})
        self.assertFalse(choose.call_args.args[-1])
        self.assertIn('描述 A', choose.call_args.args[2])
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 1, {'answers': {'q': {'answers': ['B']}}}))
        question.update(isSecret=True, isOther=False)
        with mock.patch.object(pet_panel.QInputDialog, 'getText', side_effect=[('invalid', True), ('A', True)]) as secret, \
             mock.patch.object(pet_panel.QInputDialog, 'getItem') as choose:
            self.panel._approval({'id': 2, 'method': 'item/tool/requestUserInput', 'params': {'questions': [question]}})
        self.assertEqual(secret.call_count, 2)
        self.assertTrue(all(call.args[3] == pet_panel.QLineEdit.Password for call in secret.call_args_list))
        self.assertFalse(choose.called)
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 2, {'answers': {'q': {'answers': ['A']}}}))
        question.update(isSecret=False, isOther=True)
        with mock.patch.object(pet_panel.QInputDialog, 'getItem', return_value=('自訂', True)) as choose:
            self.panel._approval({'id': 3, 'method': 'item/tool/requestUserInput', 'params': {'questions': [question]}})
        self.assertTrue(choose.call_args.args[-1])
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 3, {'answers': {'q': {'answers': ['自訂']}}}))

    def test_approval_shows_only_offered_commands_and_closes_safely(self):
        observed = []
        def reject():
            dialog = QApplication.activeModalWidget()
            observed.extend(button.text() for button in dialog.findChildren(QPushButton))
            dialog.reject()
        QTimer.singleShot(0, reject)
        self.panel._approval({'id': 1, 'method': 'item/commandExecution/requestApproval',
                              'params': {'availableDecisions': ['decline', 'cancel']}})
        self.assertNotIn('允许', observed)
        self.assertIn('拒绝', observed)
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 1, 'decline'))
        self.panel._approval({'id': 2, 'method': 'item/commandExecution/requestApproval',
                              'params': {'availableDecisions': [{'applyNetworkPolicyAmendment': {}}]}})
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 2, 'unsupported'))
        self.panel._approval({'id': 3, 'method': 'item/commandExecution/requestApproval',
                              'params': {'availableDecisions': ['accept']}})
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 3, 'unsupported'))

    def test_command_approval_renders_started_item_and_disables_blind_accept(self):
        from PyQt5.QtWidgets import QPlainTextEdit
        observed = []
        def inspect():
            dialog = QApplication.activeModalWidget()
            observed.append((dialog.findChild(QPlainTextEdit).toPlainText(),
                             [button.text() for button in dialog.findChildren(QPushButton)]))
            dialog.reject()
        QTimer.singleShot(0, inspect)
        self.panel._approval({'id': 11, 'method': 'item/commandExecution/requestApproval',
                              'params': {}, 'command_item': {'command': 'echo hello', 'cwd': '/tmp/project'}})
        self.assertIn('echo hello', observed[-1][0])
        self.assertIn('/tmp/project', observed[-1][0])
        self.assertIn('允许', observed[-1][1])
        QTimer.singleShot(0, inspect)
        self.panel._approval({'id': 12, 'method': 'item/commandExecution/requestApproval', 'params': {}})
        self.assertIn('审批详情不完整', observed[-1][0])
        self.assertNotIn('允许', observed[-1][1])
        QTimer.singleShot(0, inspect)
        self.panel._approval({'id': 13, 'method': 'item/commandExecution/requestApproval',
                              'params': {'networkApprovalContext': {'host': 'example.com', 'protocol': 'https'}}})
        self.assertIn('example.com', observed[-1][0])
        self.assertIn('允许', observed[-1][1])
        QTimer.singleShot(0, inspect)
        self.panel._approval({'id': 14, 'method': 'item/commandExecution/requestApproval',
                              'params': {'kind': 'writeStdin', 'command': 'harmless'},
                              'command_item': {'command': 'shell', 'cwd': '/tmp/project'}})
        self.assertNotIn('允许', observed[-1][1])
        self.assertIn('终端输入审批暂不支持', observed[-1][0])
        QTimer.singleShot(0, inspect)
        self.panel._approval({'id': 15, 'method': 'item/commandExecution/requestApproval',
                              'params': {'approvalId': 'subcommand-id'},
                              'command_item': {'command': 'shell', 'cwd': '/tmp/project'}})
        self.assertNotIn('允许', observed[-1][1])

    def test_additional_permission_approval_does_not_offer_unimplemented_stop(self):
        observed = []
        def reject():
            dialog = QApplication.activeModalWidget()
            observed.extend(button.text() for button in dialog.findChildren(QPushButton))
            dialog.reject()
        QTimer.singleShot(0, reject)
        self.panel._approval({'id': 4, 'method': 'item/permissions/requestApproval',
                              'params': {'permissions': {'network': {'enabled': True}}}})
        self.assertEqual(set(observed), {'允许', '拒绝'})
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 4, 'decline'))

    def test_synchronous_send_rejection_restores_draft_and_controls(self):
        self.panel.prompt.setPlainText('  原始草稿  ')
        self.panel.images = ['/tmp/example.png']
        def reject(*args):
            self.panel.bridge.received.emit('error', '已有请求正在运行。')
            return None
        self.panel.backend.send = reject
        self.panel._send()
        self.assertFalse(self.panel.busy)
        self.assertTrue(self.panel.send_button.isEnabled())
        self.assertFalse(self.panel.cancel_button.isEnabled())
        self.assertEqual(self.panel.prompt.toPlainText(), '  原始草稿  ')
        self.assertEqual(self.panel.images, ['/tmp/example.png'])
        self.assertIsNone(self.panel.inflight_draft)
        self.assertIn('已有请求正在运行', self.panel.status.text())

    def test_synchronous_rejection_preserves_new_draft_and_completed_signal(self):
        self.panel.prompt.setPlainText('原始草稿')
        def reject_after_edit(*args):
            self.panel.prompt.setPlainText('新草稿')
            self.panel.bridge.received.emit('error', '拒绝')
            return None
        self.panel.backend.send = reject_after_edit
        self.panel._send()
        self.assertEqual(self.panel.prompt.toPlainText(), '新草稿')
        self.assertEqual(self.panel.failed_drafts[0]['text'], '原始草稿')
        self.assertFalse(self.panel.busy)

        def complete_synchronously(*args):
            self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'completed'})
            return None
        self.panel.backend.send = complete_synchronously
        self.panel._send()
        self.assertEqual(self.panel.status.text(), '完成')
        self.assertEqual(len(self.panel.failed_drafts), 1)
        self.assertFalse(self.panel.busy)

    def test_file_approval_displays_matched_diff_or_unavailable_reason(self):
        from PyQt5.QtWidgets import QPlainTextEdit
        observed = []
        def reject():
            dialog = QApplication.activeModalWidget()
            observed.append((dialog.findChild(QPlainTextEdit).toPlainText(),
                             [button.text() for button in dialog.findChildren(QPushButton)]))
            dialog.reject()
        changes = [{'path': 'app.py', 'kind': {'type': 'update'}, 'diff': '-old\n+new'}]
        QTimer.singleShot(0, reject)
        self.panel._approval({'id': 10, 'method': 'item/fileChange/requestApproval',
                              'params': {'threadId': 't', 'turnId': 'r', 'itemId': 'i'}, 'file_changes': changes})
        self.assertIn('app.py', observed[-1][0])
        self.assertIn('-old\n+new', observed[-1][0])
        self.assertIn('允许', observed[-1][1])
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 10, 'decline'))
        QTimer.singleShot(0, reject)
        self.panel._approval({'id': 11, 'method': 'item/fileChange/requestApproval', 'params': {}})
        self.assertIn('文件差异不可用', observed[-1][0])
        self.assertIn('拒绝本次请求，再在 Codex 中重新执行', observed[-1][0])
        self.assertNotIn('允许', observed[-1][1])
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 11, 'decline'))

    def test_switching_modes_cancels_deferred_old_history(self):
        from PyQt5.QtGui import QTextCursor
        self.panel.mode.setCurrentIndex(2)
        transcript = self.panel.transcripts['task']
        transcript.clear()
        old = transcript.add_message('assistant', '选中的任务消息')
        cursor = old.textCursor()
        cursor.select(QTextCursor.Document)
        old.setTextCursor(cursor)
        transcript.replace_messages_preserving_scroll([('assistant', '旧的延迟刷新')])
        self.assertIsNotNone(transcript.pending_messages)
        self.panel.mode.setCurrentIndex(0)
        cursor.clearSelection()
        old.setTextCursor(cursor)
        QTest.qWait(20)
        self.assertIn('选中的任务消息', transcript.toPlainText())
        self.assertNotIn('旧的延迟刷新', transcript.toPlainText())

    def test_refresh_selected_thread_preserves_draft_and_permissions(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'chosen'
        self.panel._show_permission()
        self.panel.permission.setCurrentIndex(1)
        permission = self.panel.permission.currentData()
        self.panel.prompt.setPlainText('尚未發送的草稿')
        self.panel.images = ['/tmp/pending.png']
        self.panel.backend.calls.clear()
        self.panel._refresh_thread()
        self.assertEqual(self.panel.backend.calls, [('codex_thread', 'chosen')])
        self.panel._send()
        self.assertEqual(self.panel.backend.calls, [('codex_thread', 'chosen')])
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp', 'turns': [
            {'items': [{'type': 'agentMessage', 'text': '其他客戶端新消息'}]}]}})
        self.assertIn('其他客戶端新消息', self.panel.transcripts['task'].toPlainText())
        self.assertEqual(self.panel.prompt.toPlainText(), '尚未發送的草稿')
        self.assertEqual(self.panel.images, ['/tmp/pending.png'])
        self.assertEqual(self.panel.permission.currentData(), permission)
        self.assertFalse(self.panel.thread_loading)
        self.panel._refresh_thread()
        self.panel.bridge.received.emit('codex_thread_error', '讀取失敗')
        self.assertFalse(self.panel.thread_loading)
        self.assertIn('讀取失敗', self.panel.status.text())
        self.assertIn('其他客戶端新消息', self.panel.transcripts['task'].toPlainText())

    def test_refresh_while_busy_or_stale_cannot_replace_history(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'chosen'
        self.panel._show_permission()
        self.panel.transcripts['task'].add_message('assistant', '原有內容')
        self.panel.backend.calls.clear()
        self.panel.busy = True
        self.panel._refresh_thread()
        self.assertFalse(self.panel.backend.calls)
        self.panel.busy = False
        self.panel._refresh_thread()
        self.panel.selected_thread = 'other'
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp', 'turns': [
            {'items': [{'type': 'agentMessage', 'text': '不應顯示'}]}]}})
        self.assertIn('原有內容', self.panel.transcripts['task'].toPlainText())
        self.assertNotIn('不應顯示', self.panel.transcripts['task'].toPlainText())
        self.assertIn('刷新失败', self.panel.status.text())

    def test_refresh_malformed_history_and_mode_switch_preserve_state(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'chosen'
        self.panel._show_models('task')
        self.panel.model.setCurrentIndex(self.panel.model.findData({'id': 'model', 'name': 'Model',
            'efforts': ['low', 'high'], 'default_effort': 'high'}))
        selected_model = self.panel.model_choice.get('task')
        self.assertEqual(selected_model, 'model')
        self.panel.permission.setCurrentIndex(1)
        permission = self.panel.permission.currentData()
        self.panel.prompt.setPlainText('保留草稿')
        self.panel.transcripts['task'].add_message('assistant', '原有內容')
        self.panel._refresh_thread()
        self.panel.mode.setCurrentIndex(0)
        self.panel._choose_cwd()
        self.assertEqual(self.panel.cwd, '/tmp')
        self.panel._refresh_thread()
        self.assertEqual(len([c for c in self.panel.backend.calls if c[0] == 'codex_thread']), 1)
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp',
                                                                  'turns': [{'items': None}]}})
        self.assertIn('原有內容', self.panel.transcripts['task'].toPlainText())
        self.assertEqual(self.panel.prompt.toPlainText(), '保留草稿')
        self.panel.mode.setCurrentIndex(2)
        self.assertEqual(self.panel.permission.currentData(), permission)
        self.assertEqual(self.panel.model_choice.get('task'), selected_model)
        self.assertFalse(self.panel.thread_loading)
        self.panel._refresh_thread()
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp',
            'turns': [{'items': [{'type': 'agentMessage', 'text': '刷新後的內容'}]}]}})
        self.assertIn('刷新後的內容', self.panel.transcripts['task'].toPlainText())
        self.assertEqual(self.panel.prompt.toPlainText(), '保留草稿')
        self.assertEqual(self.panel.permission.currentData(), permission)
        self.assertEqual(self.panel.model_choice.get('task'), selected_model)

    def test_auto_follow_requires_visible_selected_idle_and_can_disable(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.backend.calls.clear()
        self.panel._follow_thread_tick()
        self.assertFalse(self.panel.backend.calls)
        self.panel.selected_thread = 'chosen'
        self.panel._follow_thread_tick()
        self.assertFalse(self.panel.backend.calls)
        self.panel.show()
        self.app.processEvents()
        self.panel.busy = True
        self.panel._follow_thread_tick()
        self.assertFalse(self.panel.backend.calls)
        self.panel.busy = False
        self.panel.thread_follow.setChecked(False)
        self.panel._follow_thread_tick()
        self.assertFalse(self.panel.backend.calls)
        self.panel.thread_follow.setChecked(True)
        self.panel._follow_thread_tick()
        self.panel._follow_thread_tick()
        self.assertEqual(self.panel.backend.calls, [('codex_thread', 'chosen')])
        self.assertFalse(self.panel.send_button.isEnabled())
        self.panel.prompt.setPlainText('保留草稿')
        self.panel.thread_follow.setChecked(False)
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp',
            'turns': [{'items': [{'type': 'agentMessage', 'text': '不應顯示'}]}]}})
        self.assertNotIn('不應顯示', self.panel.transcripts['task'].toPlainText())
        self.assertFalse(self.panel.thread_loading)
        self.assertTrue(self.panel.send_button.isEnabled())
        self.assertEqual(self.panel.prompt.toPlainText(), '保留草稿')

    def test_auto_follow_discards_result_when_hidden_or_mode_switched(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'chosen'
        self.panel.show()
        self.app.processEvents()
        self.panel._follow_thread_tick()
        self.panel.hide()
        self.panel.show()
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp',
            'turns': [{'items': [{'type': 'agentMessage', 'text': '隱藏後過期'}]}]}})
        self.assertNotIn('隱藏後過期', self.panel.transcripts['task'].toPlainText())
        self.panel._follow_thread_tick()
        self.panel.mode.setCurrentIndex(0)
        self.panel.mode.setCurrentIndex(2)
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp',
            'turns': [{'items': [{'type': 'agentMessage', 'text': '模式切換後過期'}]}]}})
        self.assertNotIn('模式切換後過期', self.panel.transcripts['task'].toPlainText())
        self.assertFalse(self.panel.thread_loading)

    def test_auto_follow_errors_do_not_repeat_and_recovery_is_reported(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'chosen'
        self.panel.show()
        self.app.processEvents()
        self.panel._follow_thread_tick()
        self.panel.bridge.received.emit('codex_thread_error', '私有故障詳情')
        self.assertIn('自动跟随暂不可用', self.panel.status.text())
        self.assertNotIn('私有故障詳情', self.panel.status.text())
        self.panel.status.setText('其他操作狀態')
        self.panel._follow_thread_tick()
        self.panel.bridge.received.emit('codex_thread_error', '重複')
        self.assertEqual(self.panel.status.text(), '其他操作狀態')
        self.panel._follow_thread_tick()
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp', 'turns': []}})
        self.assertIn('已恢复', self.panel.status.text())

    def test_refresh_preserves_scrollback_and_draft(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'chosen'
        self.panel.show()
        self.panel.resize(550, 650)
        transcript = self.panel.transcripts['task']
        transcript.clear()
        for i in range(30):
            transcript.add_message('assistant', '消息 %d ' % i + '長文字' * 40)
        QTest.qWait(100)
        bar = transcript.verticalScrollBar()
        self.assertGreater(bar.maximum(), 50)
        bar.setValue(30)
        self.panel.prompt.setPlainText('我的草稿')
        self.panel._refresh_thread()
        items = [{'type': 'agentMessage', 'text': '消息 %d ' % i + '長文字' * 40} for i in range(31)]
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp', 'turns': [{'items': items}]}})
        QTest.qWait(100)
        self.assertEqual(bar.value(), 30)
        self.assertEqual(self.panel.prompt.toPlainText(), '我的草稿')
        self.assertIn('消息 30', transcript.toPlainText())
        bar.setValue(bar.maximum())
        self.panel._refresh_thread()
        items.append({'type': 'agentMessage', 'text': '最新消息' * 80})
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'chosen', 'cwd': '/tmp', 'turns': [{'items': items}]}})
        QTest.qWait(100)
        self.assertEqual(bar.value(), bar.maximum())

    def test_bridge_worker_can_finish_after_qt_receiver_destroyed(self):
        bridge = pet_panel._EventBridge()
        emit = bridge.safe_emit
        sip.delete(bridge)
        emit('codex_thread', {'synthetic': 'ignored'})

    def test_scroll_restore_after_widget_destruction_is_ignored(self):
        transcript = pet_panel.ChatTranscript()
        transcript.replace_messages_preserving_scroll([('assistant', '合成內容')])
        sip.delete(transcript)
        self.app.processEvents()

    def test_auto_follow_timer_observes_external_append_end_to_end(self):
        class LiveBackend(FakeBackend):
            def __init__(self, emit):
                super().__init__(emit)
                self.external = ['初始合成消息']
            def codex_thread(self, thread_id):
                self.calls.append(('codex_thread', thread_id))
                snapshot = list(self.external)
                QTimer.singleShot(8, lambda: self.emit('codex_thread', {'thread': {
                    'id': thread_id, 'cwd': '/tmp', 'turns': [{'items': [
                        {'type': 'agentMessage', 'text': text} for text in snapshot]}]}}))
        panel = PetPanel(backend_factory=LiveBackend)
        panel.model_cache = {mode: [{'id': 'model', 'name': 'Model'}] for mode in ('chat', 'plan', 'task')}
        panel.mode.setCurrentIndex(2)
        panel.cwd = '/tmp'
        panel.selected_thread = 'selected-synthetic'
        panel.prompt.setPlainText('未發送的草稿')
        panel.images = ['/tmp/draft.png']
        panel.thread_follow_timer.setInterval(30)
        panel.show()
        QTimer.singleShot(70, lambda: panel.backend.external.append('外部合成新消息'))
        QTest.qWait(200)
        self.assertIn('初始合成消息', panel.transcripts['task'].toPlainText())
        self.assertIn('外部合成新消息', panel.transcripts['task'].toPlainText())
        self.assertEqual(panel.prompt.toPlainText(), '未發送的草稿')
        self.assertEqual(panel.images, ['/tmp/draft.png'])
        self.assertGreaterEqual(len(panel.backend.calls), 2)
        self.assertTrue(all(call == ('codex_thread', 'selected-synthetic') for call in panel.backend.calls))
        panel.hide()
        QTest.qWait(30)
        count = len(panel.backend.calls)
        QTest.qWait(90)
        self.assertEqual(len(panel.backend.calls), count)
        panel.thread_follow_timer.stop()
        panel.deleteLater()
        self.app.processEvents()

    def test_chat_progress_and_error_completion(self):
        self.panel.prompt.setPlainText('你好')
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'chat', '你好', None, 'model', None, None, None, None))
        self.panel.bridge.received.emit('delta', '您好')
        self.assertIn('您好', self.panel.transcript.toPlainText())
        self.panel.bridge.received.emit('error', '授權失敗')
        self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'failed'})
        self.assertIn('授權失敗', self.panel.status.text())
        self.assertTrue(self.panel.send_button.isEnabled())

    def test_task_completion_drives_character_and_uses_directory(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.prompt.setPlainText('檢查')
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'task', '檢查', '/tmp', 'model', None, None, None, None))
        self.assertEqual(self.actions[-1], 'work')
        self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'completed'})
        self.assertEqual(self.actions[-1], 'celebrate')

    def test_automatic_follow_accepts_symlinked_work_directory_and_keeps_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'real').mkdir()
            (root / 'alias').symlink_to(root / 'real', target_is_directory=True)
            self.panel.mode.setCurrentIndex(2)
            self.panel.cwd = str(root / 'alias')
            self.panel.show()
            self.panel.transcripts['plan'].add_message('assistant', '保留计划')
            self.panel.prompt.setPlainText('检查')
            self.panel._send()
            request_id = self.panel.inflight_draft['request_id']
            self.panel.bridge.received.emit('thread', {'mode': 'task', 'request_id': request_id,
                                            'thread': {'id': 'new', 'cwd': str(root / 'real')}})
            self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'completed'})
            self.panel._follow_thread_tick()
            self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'new', 'cwd': str(root / 'real'),
                'turns': [{'items': [{'type': 'agentMessage', 'text': '更新'}]}]}})
            self.assertFalse(self.panel.thread_follow_failed)
            self.assertIn('更新', self.panel.transcripts['task'].toPlainText())
            self.assertIn('保留计划', self.panel.transcripts['plan'].toPlainText())
            self.assertNotIn(('new_thread', 'plan', str(root / 'real')), self.panel.backend.calls)

    def test_new_task_follows_its_thread_after_completion_without_changing_permissions(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.show()
        self.panel.permission.setCurrentIndex(0)
        self.panel.prompt.setPlainText('检查')
        self.panel._send()
        request_id = self.panel.inflight_draft['request_id']
        self.panel.bridge.received.emit('thread', {'mode': 'task', 'request_id': request_id,
                                         'thread': {'id': 'new-task', 'cwd': '/tmp'}})
        self.assertEqual(self.panel.selected_thread, 'new-task')
        self.assertIsNone(self.panel.permission.currentData())
        self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'completed'})
        self.panel._follow_thread_tick()
        self.assertEqual(self.panel.backend.calls[-1], ('codex_thread', 'new-task'))

    def test_thread_event_rejects_stale_request_wrong_mode_cwd_or_user_selection(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.prompt.setPlainText('检查')
        self.panel._send()
        ident = self.panel.inflight_draft['request_id']
        event = {'mode': 'task', 'request_id': ident, 'thread': {'id': 'new-task', 'cwd': '/tmp'}}
        for altered in (None, {**event, 'request_id': 'old'}, {**event, 'mode': 'plan'},
                        {**event, 'thread': {'id': 'new-task', 'cwd': '/'}},
                        {**event, 'thread': {'id': '', 'cwd': '/tmp'}},
                        {**event, 'thread': {'id': 'new-task'}}):
            self.panel.bridge.received.emit('thread', altered)
            self.assertIsNone(self.panel.selected_thread)
        self.panel.selected_thread = 'manually-selected'
        self.panel.bridge.received.emit('thread', event)
        self.assertEqual(self.panel.selected_thread, 'manually-selected')
        self.panel.selected_thread = None
        self.panel.cwd = '/'
        self.panel.bridge.received.emit('thread', event)
        self.assertIsNone(self.panel.selected_thread)
        self.panel.cwd = '/tmp'
        self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'completed'})
        self.panel.bridge.received.emit('thread', event)
        self.assertIsNone(self.panel.selected_thread)

    def test_background_task_thread_event_is_correlated_before_auto_follow(self):
        from codex_backend import Backend
        class SyntheticBackend(Backend):
            def _agent(self, mode, text, cwd, *args, request_id=None, **kwargs):
                self.emit('thread', {'mode': mode, 'request_id': request_id,
                                     'thread': {'id': 'new-background', 'cwd': cwd}})
                return 'completed'
        panel = PetPanel(backend_factory=lambda emit: SyntheticBackend(emit, auth=object()))
        panel.model_cache = {'task': [{'id': 'model', 'name': 'Model'}]}
        panel.mode.setCurrentIndex(2)
        panel.cwd = '/tmp'
        panel.show()
        panel.prompt.setPlainText('检查')
        panel._send()
        for _ in range(30):
            self.app.processEvents()
            if not panel.busy:
                break
            QTest.qWait(10)
        self.assertFalse(panel.busy)
        self.assertEqual(panel.selected_thread, 'new-background')
        self.assertIsNone(panel.permission.currentData())
        panel.backend.close()
        panel.hide()
        panel.deleteLater()

    def test_bubbles_preserve_plain_text_stream_and_scroll(self):
        self.panel.resize(500, 580)
        self.panel.show()
        self.panel.prompt.setPlainText('<b>普通文字</b>')
        self.panel._send()
        self.panel.bridge.received.emit('delta', '第一段\n')
        self.panel.bridge.received.emit('delta', '很長的回覆。' * 300)
        QTest.qWait(100)
        transcript = self.panel.transcript
        self.assertEqual(transcript.bubbles[-2].toPlainText(), '<b>普通文字</b>')
        self.assertTrue(transcript.bubbles[-1].raw_text.startswith('第一段\n'))
        self.assertGreater(transcript.verticalScrollBar().maximum(), 0)
        self.assertEqual(transcript.verticalScrollBar().value(), transcript.verticalScrollBar().maximum())
        self.panel.bridge.received.emit('delta', '結尾' * 300)
        transcript.verticalScrollBar().setValue(0)
        QTest.qWait(100)
        self.assertEqual(transcript.verticalScrollBar().value(), 0)

    def test_long_tokens_wrap_and_background_errors_are_separate(self):
        self.panel.resize(500, 580)
        self.panel.show()
        self.panel.prompt.setPlainText('請解釋')
        self.panel._send()
        self.panel.bridge.received.emit('delta', 'x' * 2000)
        self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'completed'})
        QTest.qWait(100)
        transcript = self.panel.transcript
        self.assertEqual(transcript.horizontalScrollBar().maximum(), 0)
        bubble = transcript.bubbles[-1]
        self.assertLessEqual(bubble.document().size().height(), bubble.viewport().height())
        old_reply = transcript.bubbles[-1].toPlainText()
        self.panel.bridge.received.emit('error', '網路問題' * 100)
        QTest.qWait(100)
        self.assertEqual(transcript.bubbles[-2].toPlainText(), old_reply)
        self.assertTrue(transcript.bubbles[-1].toPlainText().startswith('[错误]'))
        self.assertLessEqual(self.panel.status.height(), 38)
        self.assertIn('網路問題', self.panel.status.toolTip())

    def test_installed_avatar_is_found_next_to_script(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            source = Path(__file__).resolve().parents[1] / 'assets' / 'dragon-seated.png'
            shutil.copyfile(source, target / 'dragon-seated.png')
            with mock.patch.object(pet_panel, '__file__', str(target / 'pet_panel.py')):
                panel = PetPanel(backend_factory=FakeBackend)
            avatar = panel.findChild(QLabel, 'avatar')
            self.assertIsNotNone(avatar.pixmap())
            self.assertFalse(avatar.pixmap().isNull())
            panel.deleteLater()

    def test_quotas_and_unknown_approvals_fail_closed(self):
        self.panel.refresh_limits()
        self.assertEqual(self.panel.backend.calls, [('account',), ('limits',)])
        self.panel.bridge.received.emit('limits', {'rateLimits': {'primary': {'usedPercent': 25, 'windowDurationMins': 300}}})
        self.assertIn('25%', self.panel.limit_text.text())
        self.assertEqual(self.panel.usage_bars[0][2].value(), 25)
        self.assertFalse(self.panel.usage_bars[0][0].isHidden())
        self.assertTrue(self.panel.usage_bars[1][0].isHidden())
        self.assertIn('不可用', self.panel.limit_text.text())
        self.panel.bridge.received.emit('approval', {'id': 9, 'method': 'unknown', 'params': {}})
        self.assertEqual(self.panel.backend.calls[-1], ('approve', 9, 'decline'))

    def test_quota_events_are_isolated_from_chat_and_refresh_is_coalesced(self):
        events = []
        self.panel.limits_changed.connect(lambda event, data: events.append((event, data)))
        self.panel.prompt.setPlainText('你好')
        self.panel._send()
        before = self.panel.transcript.toPlainText()
        self.assertTrue(self.panel.refresh_limits(include_account=False))
        self.assertFalse(self.panel.refresh_limits(include_account=False))
        self.assertEqual(self.panel.backend.calls.count(('limits',)), 1)
        self.panel.bridge.received.emit('limits_error', '额度查询出错')
        self.assertEqual(self.panel.transcript.toPlainText(), before)
        self.assertTrue(self.panel.busy)
        self.assertFalse(self.panel.limits_inflight)
        self.assertEqual(events[-1], ('limits_error', '额度查询出错'))
        self.assertIn('额度查询出错', self.panel.limit_text.text())
        self.assertTrue(self.panel.refresh_limits(include_account=False))
        self.panel.bridge.received.emit('error', '聊天出错')
        self.assertTrue(self.panel.limits_inflight)
        self.assertEqual(events[-1][0], 'loading')
        self.panel.bridge.received.emit('limits', {'rateLimits': {'primary': {'usedPercent': 20}}})
        self.assertFalse(self.panel.limits_inflight)
        self.assertEqual(events[-1][0], 'limits')
        self.assertIn('20%', self.panel.limit_text.text())

    def test_quota_signal_is_delivered_to_qt_thread(self):
        delivered = []
        main_thread = threading.get_ident()
        self.panel.limits_changed.connect(lambda *event: delivered.append((threading.get_ident(), event)))
        worker = threading.Thread(target=lambda: self.panel.bridge.received.emit('limits', {}))
        worker.start()
        worker.join()
        QTest.qWait(30)
        self.assertEqual(delivered, [(main_thread, ('limits', {}))])

    def test_small_quota_page_keeps_controls_reachable(self):
        self.panel.resize(480, 580)
        self.panel.show()
        self.panel.tabs.setCurrentIndex(1)
        self.panel._show_limits({'rateLimits': {
            'primary': {'usedPercent': 27, 'windowDurationMins': 300, 'resetsAt': 1793278800},
            'secondary': {'usedPercent': 63, 'windowDurationMins': 10080, 'resetsAt': 1793700000},
        }})
        QTest.qWait(30)
        scroll = self.panel.tabs.currentWidget()
        refresh = next(button for button in scroll.findChildren(QPushButton) if button.text() == '刷新额度')
        scroll.ensureWidgetVisible(refresh)
        QTest.qWait(20)
        self.assertTrue(scroll.viewport().rect().contains(refresh.mapTo(scroll.viewport(), refresh.rect().center())))

    def test_small_chat_keeps_controls_and_transcript_reachable(self):
        self.panel.resize(480, 580)
        self.panel.mode.setCurrentIndex(2)
        self.panel.show()
        QTest.qWait(30)
        self.assertTrue(self.panel.hero.isHidden())
        self.assertGreaterEqual(self.panel.transcript.height(), 120)
        for widget in (self.panel.mode, self.panel.model, self.panel.effort,
                       self.panel.new_button, self.panel.threads_button, self.panel.send_button):
            self.assertTrue(self.panel.rect().contains(widget.mapTo(self.panel, widget.rect().center())))

    def test_models_update_per_mode_and_error_does_not_silently_pick_another(self):
        self.panel.model_cache = {}
        self.panel._refresh_models()
        self.assertEqual(self.panel.backend.calls[-1], ('models', 'chat', 1))
        self.panel.mode.setCurrentIndex(2)
        self.assertEqual(self.panel.backend.calls[-1], ('models', 'task', 1))
        self.panel.bridge.received.emit('models', {'mode': 'chat', 'generation': 1, 'models': [{'id': 'chat-id', 'name': 'Chat'}]})
        self.assertIsNone(self.panel.model.currentData())
        self.panel.bridge.received.emit('models', {'mode': 'task', 'generation': 1, 'models': [
            {'id': 'task-id', 'name': 'Task', 'efforts': ['low', 'high'], 'default_effort': 'high'}]})
        self.assertEqual(self.panel.model.currentData()['id'], 'task-id')
        self.assertIsNone(self.panel.effort.currentData())
        self.panel.effort.setCurrentIndex(self.panel.effort.findData('high'))
        self.assertEqual(self.panel.effort.currentData(), 'high')
        self.panel.mode.setCurrentIndex(0)
        self.assertEqual(self.panel.model.currentData()['id'], 'chat-id')
        self.panel.model_cache.pop('chat')
        self.panel._refresh_models()
        self.panel.bridge.received.emit('models_error', {'mode': 'chat', 'generation': 2, 'message': '請登入'})
        self.assertIsNone(self.panel.model.currentData())
        self.panel.prompt.setPlainText('hi')
        self.panel._send()
        self.assertFalse(self.panel.busy)
        self.assertIn('模型', self.panel.status.text())

    def test_model_and_effort_choices_survive_mode_switch_and_model_refresh(self):
        self.panel.model_cache['chat'] = [{'id': 'c1', 'name': 'First'}, {'id': 'c2', 'name': 'Second'}]
        self.panel._show_models('chat')
        self.panel.model.setCurrentIndex(1)
        self.panel.mode.setCurrentIndex(2)
        self.panel.effort.setCurrentIndex(self.panel.effort.findData('high'))
        self.panel.mode.setCurrentIndex(0)
        self.assertEqual(self.panel.model.currentData()['id'], 'c2')
        self.panel.bridge.received.emit('models', {'mode': 'chat', 'generation': 0, 'models': self.panel.model_cache['chat']})
        self.assertEqual(self.panel.model.currentData()['id'], 'c2')
        self.panel.mode.setCurrentIndex(2)
        self.assertEqual(self.panel.effort.currentData(), 'high')

    def test_switching_directory_clears_history_model_and_plan_transcript(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/old'
        self.panel.selected_thread = 'history'
        self.panel.model_choice['task'] = '__thread__'
        self.panel._show_models('task')
        self.panel.transcripts['plan'].add_message('user', 'old plan')
        with mock.patch.object(pet_panel.QFileDialog, 'getExistingDirectory', return_value='/new'):
            self.panel._choose_cwd()
        self.assertIsNone(self.panel.selected_thread)
        self.assertEqual(self.panel.model.currentData()['id'], 'model')
        self.assertNotIn('old plan', self.panel.transcripts['plan'].toPlainText())

    def test_loading_history_in_different_directory_clears_plan_context(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/old'
        self.panel.transcripts['plan'].add_message('user', 'old plan')
        self.panel._open_threads()
        self.panel.bridge.received.emit('codex_threads', {'data': [{'id': 'two', 'cwd': '/new'}]})
        self.panel.thread_list.setCurrentRow(0)
        self.panel.thread_open.click()
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'two', 'cwd': '/new', 'turns': []}})
        self.assertEqual(self.panel.cwd, '/new')
        self.assertNotIn('old plan', self.panel.transcripts['plan'].toPlainText())
        self.assertIn(('new_thread', 'plan', '/new'), self.panel.backend.calls)

    def test_successful_login_invalidates_models_and_ignores_old_results(self):
        self.panel._refresh_models()
        self.assertEqual(self.panel.model_generation['chat'], 1)
        self.panel.transcripts['chat'].add_message('user', 'old account chat')
        self.panel.backend_login()
        self.assertTrue(self.panel.login_inflight)
        self.panel.prompt.setPlainText('wait')
        self.panel._send()
        self.assertFalse(self.panel.busy)
        self.panel.bridge.received.emit('login_complete', None)
        self.assertFalse(self.panel.login_inflight)
        self.assertNotIn('old account chat', self.panel.transcripts['chat'].toPlainText())
        self.assertNotIn('chat', self.panel.model_cache)
        self.assertNotIn('plan', self.panel.model_cache)
        self.assertEqual(self.panel.backend.calls[-2:], [('models', 'chat', 3), ('models', 'plan', 2)])
        self.panel.bridge.received.emit('models', {'mode': 'chat', 'generation': 1,
                                                   'models': [{'id': 'old', 'name': 'Old'}]})
        self.assertNotIn('chat', self.panel.model_cache)
        self.assertIn('chat', self.panel.models_loading)
        self.panel.bridge.received.emit('models', {'mode': 'chat', 'generation': 3,
                                                   'models': [{'id': 'new', 'name': 'New'}]})
        self.assertEqual(self.panel.model.currentData()['id'], 'new')

    def test_failed_login_keeps_existing_account_models(self):
        self.panel.backend_login()
        self.panel.bridge.received.emit('login_error', '取消授權')
        self.assertFalse(self.panel.login_inflight)
        self.assertEqual(self.panel.model.currentData()['id'], 'model')

    def test_images_require_selection_send_and_can_be_removed(self):
        selected = ['/private/a.png', '/private/b.jpg', '/private/a.png']
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(selected, '')):
            self.panel.add_image.click()
        self.assertEqual(self.panel.images, ['/private/a.png', '/private/b.jpg'])
        self.assertEqual(self.panel.image_list.item(0).text(), 'a.png')
        self.assertFalse(any(call[0] == 'send' for call in self.panel.backend.calls))
        self.panel.image_list.setCurrentRow(0)
        self.panel.remove_image.click()
        self.assertEqual(self.panel.images, ['/private/b.jpg'])
        self.panel.prompt.setPlainText('請看圖片')
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'chat', '請看圖片', None, 'model', None, None, None, ['/private/b.jpg']))
        self.assertEqual(self.panel.images, [])
        self.assertIn('b.jpg', self.panel.transcript.toPlainText())
        self.assertNotIn('/private/', self.panel.transcript.toPlainText())
        self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'completed'})
        self.panel.prompt.setPlainText('再聊')
        self.panel._send()
        self.assertIsNone(self.panel.backend.calls[-1][-1])

    def test_image_context_disclosure_changes_with_mode(self):
        self.assertIn('后续请求会再次传送', self.panel.notice.text())
        self.assertIn('后续请求再次传送', self.panel.upload_hint.text())
        self.assertIn('新对话', self.panel.upload_hint.text())
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(['/tmp/one.png'], '')):
            self.panel.add_image.click()
        self.assertIn('后续请求再次传送', self.panel.status.text())
        for mode in ('plan', 'task'):
            self.panel.mode.setCurrentIndex(self.panel.mode.findData(mode))
            self.assertIn('Codex 线程管理', self.panel.upload_hint.text())
            self.assertIn('临时副本', self.panel.upload_hint.text())
            self.assertIn('执行结束后清理', self.panel.upload_hint.text())

    def test_cancel_or_exceeding_image_selection_does_not_send_files(self):
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=([], '')):
            self.panel.add_image.click()
        self.assertEqual(self.panel.images, [])
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames',
                               return_value=([f'/tmp/{i}.png' for i in range(5)], '')):
            self.panel.add_image.click()
        self.assertEqual(len(self.panel.images), 4)
        self.assertIn('4', self.panel.status.text())
        self.assertFalse(any(call[0] == 'send' for call in self.panel.backend.calls))

    def test_images_use_selected_task_mode_and_permission(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'history'
        self.panel.model_choice['task'] = '__thread__'
        self.panel._show_models('task')
        self.panel._show_permission()
        self.panel.permission.setCurrentIndex(self.panel.permission.findData('read-only'))
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(['/tmp/pet.webp'], '')):
            self.panel.add_image.click()
        self.panel.prompt.setPlainText('看圖')
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'task', '看圖', '/tmp', None, None, 'history', 'read-only', ['/tmp/pet.webp']))

    def test_images_are_forwarded_in_plan_mode(self):
        self.panel.mode.setCurrentIndex(1)
        self.panel.cwd = '/tmp'
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(['/tmp/plan.png'], '')):
            self.panel.add_image.click()
        self.panel.prompt.setPlainText('研究圖片')
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'plan', '研究圖片', '/tmp', 'model', None, None, None, ['/tmp/plan.png']))

    def test_image_only_message_is_sent_without_synthetic_prompt(self):
        self.panel.images = ['/tmp/one.png']
        self.panel._show_images()
        self.panel.prompt.clear()
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1],
                         ('send', 'chat', '', None, 'model', None, None, None, ['/tmp/one.png']))
        self.assertIn('[图片附件]', self.panel.transcript.toPlainText())

    def test_login_retains_images_and_requires_explicit_account_review(self):
        self.panel.images = ['/tmp/one.png']
        self.panel.prompt.setPlainText('草稿')
        self.panel._event('login_complete', None)
        self.assertEqual(self.panel.images, ['/tmp/one.png'])
        self.assertFalse(self.panel.attachment_review.isHidden())
        self.panel.model_cache['chat'] = [{'id': 'model', 'name': 'Model'}]
        self.panel._show_models('chat')
        with mock.patch.object(pet_panel.QMessageBox, 'question', return_value=pet_panel.QMessageBox.No) as question:
            self.panel._send()
            self.assertEqual(question.call_args.args[-1], pet_panel.QMessageBox.No)
        self.assertEqual(self.panel.images, ['/tmp/one.png'])
        self.assertEqual(self.panel.prompt.toPlainText(), '草稿')
        self.assertFalse(any(call[0] == 'send' for call in self.panel.backend.calls))
        with mock.patch.object(pet_panel.QMessageBox, 'question', return_value=pet_panel.QMessageBox.Yes):
            self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1][-1], ['/tmp/one.png'])

    def test_login_marks_recoverable_image_drafts_for_account_review(self):
        self.panel.failed_drafts = [{'mode': 'chat', 'cwd': None, 'text': 'old', 'images': ['/tmp/old.png']}]
        self.panel._event('login_complete', None)
        self.panel._restore_failed_draft()
        self.assertEqual(self.panel.images, ['/tmp/old.png'])
        self.assertTrue(self.panel.images_after_login_need_review)
        self.assertFalse(self.panel.attachment_review.isHidden())

    def test_failed_image_send_restores_unmodified_draft_and_does_not_resend(self):
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(['/tmp/one.png'], '')):
            self.panel.add_image.click()
        self.panel.prompt.setPlainText('  讀取圖片\n')
        self.panel._send()
        self.panel.bridge.received.emit('error', '讀取失敗')
        self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'failed'})
        self.assertEqual(self.panel.prompt.toPlainText(), '  讀取圖片\n')
        self.assertEqual(self.panel.images, ['/tmp/one.png'])
        self.assertIn('草稿已恢复', self.panel.status.text())
        self.assertEqual(sum(call[0] == 'send' for call in self.panel.backend.calls), 1)
        self.assertFalse(self.panel.busy)

    def test_interrupted_send_does_not_overwrite_new_input_and_recovery_is_explicit(self):
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(['/tmp/one.png'], '')):
            self.panel.add_image.click()
        self.panel.prompt.setPlainText('原草稿')
        self.panel._send()
        self.panel.prompt.setPlainText('後續輸入')
        self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'interrupted'})
        self.assertEqual(self.panel.prompt.toPlainText(), '後續輸入')
        self.assertEqual(self.panel.images, [])
        self.assertEqual(len(self.panel.failed_drafts), 1)
        self.assertFalse(self.panel.restore_draft.isHidden())
        self.panel.restore_draft.click()
        self.assertEqual(self.panel.prompt.toPlainText(), '後續輸入\n\n原草稿')
        self.assertEqual(self.panel.images, ['/tmp/one.png'])
        self.assertEqual(self.panel.failed_drafts, [])
        self.assertIn('可能已部分处理', self.panel.status.text())

    def test_stop_with_unknown_result_warns_and_preserves_draft(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.prompt.setPlainText('執行任務')
        self.panel.images = ['/tmp/task.png']
        self.panel._show_images()
        self.panel._send()
        self.panel.backend_cancel()
        self.assertEqual(self.panel.backend.calls[-1], ('cancel',))
        self.assertFalse(self.panel.cancel_button.isEnabled())
        self.assertIn('等待', self.panel.status.text())
        self.panel.bridge.received.emit('error', '連線中斷')
        self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'unknown'})
        self.assertIn('结果未知', self.panel.status.text())
        self.assertIn('无法确认', self.panel.status.text())
        self.assertIn('重试可能重复', self.panel.transcript.toPlainText())
        self.assertEqual(self.panel.prompt.toPlainText(), '執行任務')
        self.assertEqual(self.panel.images, ['/tmp/task.png'])
        self.assertEqual(self.actions[-1], 'idle')
        self.assertFalse(self.panel.busy)
        self.assertEqual(sum(call[0] == 'send' for call in self.panel.backend.calls), 1)

    def test_completed_after_stop_is_completion_not_restored_draft(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.prompt.setPlainText('已完成的任務')
        self.panel.images = ['/tmp/task.png']
        self.panel._show_images()
        self.panel._send()
        self.panel.backend_cancel()
        self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'completed'})
        self.assertEqual(self.panel.status.text(), '完成')
        self.assertEqual(self.panel.prompt.toPlainText(), '')
        self.assertEqual(self.panel.images, [])
        self.assertEqual(self.panel.failed_drafts, [])
        self.assertEqual(self.actions[-1], 'celebrate')

    def test_draft_recovery_preserves_inputs_when_image_limit_would_be_exceeded(self):
        self.panel.failed_drafts = [{'mode': 'chat', 'cwd': None, 'text': 'old', 'images': ['/tmp/old.png']}]
        self.panel.images = [f'/tmp/{i}.png' for i in range(4)]
        self.panel.prompt.setPlainText('new')
        self.panel._update_failed_drafts()
        self.panel.restore_draft.click()
        self.assertEqual(self.panel.prompt.toPlainText(), 'new')
        self.assertEqual(len(self.panel.images), 4)
        self.assertEqual(len(self.panel.failed_drafts), 1)
        self.assertIn('超过上限', self.panel.status.text())

    def test_small_window_keeps_selected_images_and_controls_reachable(self):
        self.panel.resize(480, 580)
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'history'
        self.panel.model_choice['task'] = '__thread__'
        self.panel._show_models('task')
        self.panel._show_permission()
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(['/tmp/one.png', '/tmp/two.jpg'], '')):
            self.panel.add_image.click()
        self.panel.show()
        self.panel._progress({'kind': 'item', 'phase': 'started', 'type': 'webSearch'})
        QTest.qWait(30)
        for widget in (self.panel.image_list, self.panel.remove_image, self.panel.add_image, self.panel.send_button):
            self.assertTrue(self.panel.rect().contains(widget.mapTo(self.panel, widget.rect().center())))
        self.assertEqual(self.panel.image_list.count(), 2)

    def test_history_image_does_not_expose_snapshot_paths(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel._open_threads()
        self.panel.bridge.received.emit('codex_threads', {'data': [{'id': 'two', 'cwd': '/tmp'}]})
        self.panel.thread_list.setCurrentRow(0)
        self.panel.thread_open.click()
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'two', 'cwd': '/tmp', 'turns': [
            {'items': [{'type': 'userMessage', 'content': [{'type': 'text', 'text': '看圖'},
              {'type': 'localImage', 'path': '/tmp/private-snapshot.png'}]}]}]}})
        self.assertIn('[图片]', self.panel.transcript.toPlainText())
        self.assertNotIn('private-snapshot', self.panel.transcript.toPlainText())

    def test_selected_history_shows_plan_and_safe_tool_summaries_in_order(self):
        self.panel.mode.setCurrentIndex(2)
        self.assertFalse(any(call[0].startswith('codex_thread') for call in self.panel.backend.calls))
        self.panel._open_threads()
        self.panel.bridge.received.emit('codex_threads', {'data': [{'id': 'selected', 'cwd': '/tmp'}]})
        self.panel.thread_list.setCurrentRow(0)
        self.panel.thread_open.click()
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'selected', 'cwd': '/tmp', 'turns': [
            {'items': [
                {'type': 'userMessage', 'content': [{'type': 'text', 'text': '工作'}]},
                {'type': 'plan', 'text': '第一步：檢查'},
                {'type': 'commandExecution', 'command': 'private command', 'aggregatedOutput': 'private output', 'status': 'completed'},
                {'type': 'fileChange', 'changes': [{'path': 'example.py', 'diff': 'private diff'}], 'status': 'completed'},
                {'type': 'mcpToolCall', 'server': 'docs', 'tool': 'lookup', 'arguments': 'private arguments'},
                {'type': 'agentMessage', 'text': '完成'}]}]}})
        text = self.panel.transcript.toPlainText()
        self.assertLess(text.index('工作'), text.index('第一步：檢查'))
        self.assertLess(text.index('第一步：檢查'), text.index('历史执行'))
        self.assertLess(text.index('example.py'), text.index('完成'))
        self.assertIn('docs / lookup', text)
        self.assertNotIn('private', text)

    def test_explicit_codex_history_selection_pagination_and_new_conversation(self):
        self.panel.mode.setCurrentIndex(2)
        self.assertFalse(any(call[0].startswith('codex_thread') for call in self.panel.backend.calls))
        self.panel._open_threads()
        self.assertEqual(self.panel.backend.calls[-1], ('codex_threads', None))
        self.panel.bridge.received.emit('codex_threads', {'data': [{'id': 'one', 'preview': '第一個', 'cwd': '/tmp'}], 'nextCursor': 'more'})
        self.panel.thread_more.click()
        self.assertEqual(self.panel.backend.calls[-1], ('codex_threads', 'more'))
        self.panel.bridge.received.emit('codex_threads', {'data': [{'id': 'two', 'preview': '第二個', 'cwd': '/tmp'}], 'nextCursor': None})
        self.panel.thread_list.setCurrentRow(1)
        self.panel.thread_open.click()
        self.assertEqual(self.panel.backend.calls[-1], ('codex_thread', 'two'))
        self.panel.bridge.received.emit('codex_thread', {'thread': {'id': 'two', 'cwd': '/tmp', 'turns': [{'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': '舊問題'}]},
            {'type': 'agentMessage', 'text': '舊回答'}]}]}})
        self.assertIn('舊問題', self.panel.transcript.toPlainText())
        self.assertIn('舊回答', self.panel.transcript.toPlainText())
        self.panel.prompt.setPlainText('繼續')
        self.panel._send()
        self.assertFalse(self.panel.busy)
        self.assertIn('选择本次执行权限', self.panel.status.text())
        self.panel.permission.setCurrentIndex(self.panel.permission.findData('read-only'))
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'task', '繼續', '/tmp', None, None, 'two', 'read-only', None))
        self.assertFalse(self.panel.mode.isEnabled())
        self.assertFalse(self.panel.choose_cwd.isEnabled())
        self.assertFalse(self.panel.permission.isEnabled())
        self.panel.mode.setCurrentIndex(0)
        self.assertEqual(self.panel.mode.currentData(), 'task')
        self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'completed'})
        self.panel._new_conversation()
        self.assertEqual(self.panel.backend.calls[-1], ('new_thread', 'task', '/tmp'))
        self.assertNotIn('舊問題', self.panel.transcript.toPlainText())
        self.assertIsNone(self.panel.selected_thread)

    def test_selected_history_changes_model_and_effort_only_on_explicit_choice(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'existing'
        self.panel.model_choice['task'] = '__thread__'
        self.panel._show_models('task')
        self.assertTrue(self.panel.model.currentData()['follow_thread'])
        self.assertIsNone(self.panel.effort.currentData())
        self.panel.model.setCurrentIndex(1)
        self.panel.effort.setCurrentIndex(self.panel.effort.findData('high'))
        self.panel.permission.setCurrentIndex(self.panel.permission.findData('workspace-write'))
        self.panel.prompt.setPlainText('繼續')
        self.panel._send()
        self.assertEqual(self.panel.backend.calls[-1], ('send', 'task', '繼續', '/tmp', 'model', 'high', 'existing', 'workspace-write', None))

    def test_progress_stream_and_model_reroute_are_readable(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.prompt.setPlainText('執行')
        self.panel._send()
        events = [
            {'kind': 'item', 'phase': 'started', 'type': 'commandExecution'},
            {'kind': 'plan_delta', 'item_id': 'p', 'delta': '第一'},
            {'kind': 'plan_delta', 'item_id': 'p', 'delta': '步'},
            {'kind': 'plan', 'steps': [{'step': '檢查', 'status': 'inProgress'}]},
            {'kind': 'model_rerouted', 'from_model': 'a', 'to_model': 'b', 'reason': '服務調整'},
            {'kind': 'plan_final', 'item_id': 'p', 'text': '第一步'},
        ]
        for event in events:
            self.panel.bridge.received.emit('progress', event)
        self.assertFalse(self.panel.progress_panel.isHidden())
        text = self.panel.progress_log.toPlainText()
        for fragment in ('开始：命令', '第一步', '檢查', 'a → b', '服務調整'):
            self.assertIn(fragment, text)
        self.assertEqual(text.count('第一步'), 1)
        self.panel.progress_toggle.click()
        self.assertTrue(self.panel.progress_log.isHidden())
        self.panel.progress_toggle.click()
        self.assertFalse(self.panel.progress_log.isHidden())
        self.panel.bridge.received.emit('done', {'mode': 'task', 'status': 'completed'})
        self.panel.mode.setCurrentIndex(0)
        self.assertTrue(self.panel.progress_panel.isHidden())
        self.panel.mode.setCurrentIndex(2)
        self.assertFalse(self.panel.progress_panel.isHidden())

    def test_small_history_permission_is_accessible_with_progress(self):
        self.panel.resize(480, 580)
        self.panel.mode.setCurrentIndex(2)
        self.panel.cwd = '/tmp'
        self.panel.selected_thread = 'history'
        self.panel._show_models('task')
        self.panel._show_permission()
        self.panel.show()
        self.panel._progress({'kind': 'item', 'phase': 'started', 'type': 'webSearch'})
        self.panel.prompt.setPlainText('繼續')
        self.panel._send()
        QTest.qWait(30)
        scroll = self.panel.options_scroll
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        self.assertTrue(scroll.viewport().rect().contains(
            self.panel.permission.mapTo(scroll.viewport(), self.panel.permission.rect().center())))
        self.assertIn('选择本次执行权限', self.panel.status.text())

    def test_closed_picker_waits_for_inflight_result_before_reopen(self):
        self.panel.mode.setCurrentIndex(2)
        self.panel._open_threads()
        self.panel.thread_dialog.reject()
        self.panel._open_threads()
        self.assertIsNone(self.panel.thread_dialog)
        self.panel.bridge.received.emit('codex_threads', {'data': [], 'nextCursor': None})
        self.panel._open_threads()
        self.assertIsNotNone(self.panel.thread_dialog)
        self.panel.thread_dialog.reject()


if __name__ == '__main__':
    unittest.main()
