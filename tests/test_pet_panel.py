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

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton
import pet_panel
from pet_panel import PetPanel


class FakeBackend:
    def __init__(self, emit):
        self.emit = emit
        self.calls = []

    def login(self): self.calls.append(('login',))
    def account(self): self.calls.append(('account',))
    def limits(self): self.calls.append(('limits',))
    def send(self, *args): self.calls.append(('send', *args))
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
        self.assertTrue(transcript.bubbles[-1].toPlainText().startswith('第一段\n'))
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
        self.assertTrue(transcript.bubbles[-1].toPlainText().startswith('[錯誤]'))
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
        refresh = next(button for button in scroll.findChildren(QPushButton) if button.text() == '刷新額度')
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
        self.assertIn('後續請求會再次傳送', self.panel.notice.text())
        self.assertIn('後續請求再次傳送', self.panel.upload_hint.text())
        self.assertIn('新對話', self.panel.upload_hint.text())
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(['/tmp/one.png'], '')):
            self.panel.add_image.click()
        self.assertIn('後續請求再次傳送', self.panel.status.text())
        for mode in ('plan', 'task'):
            self.panel.mode.setCurrentIndex(self.panel.mode.findData(mode))
            self.assertIn('Codex 線程管理', self.panel.upload_hint.text())
            self.assertIn('臨時副本', self.panel.upload_hint.text())
            self.assertIn('執行結束後清理', self.panel.upload_hint.text())

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

    def test_failed_image_send_restores_unmodified_draft_and_does_not_resend(self):
        with mock.patch.object(pet_panel.QFileDialog, 'getOpenFileNames', return_value=(['/tmp/one.png'], '')):
            self.panel.add_image.click()
        self.panel.prompt.setPlainText('  讀取圖片\n')
        self.panel._send()
        self.panel.bridge.received.emit('error', '讀取失敗')
        self.panel.bridge.received.emit('done', {'mode': 'chat', 'status': 'failed'})
        self.assertEqual(self.panel.prompt.toPlainText(), '  讀取圖片\n')
        self.assertEqual(self.panel.images, ['/tmp/one.png'])
        self.assertIn('草稿已恢復', self.panel.status.text())
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
        self.assertIn('可能已部分處理', self.panel.status.text())

    def test_draft_recovery_preserves_inputs_when_image_limit_would_be_exceeded(self):
        self.panel.failed_drafts = [{'mode': 'chat', 'cwd': None, 'text': 'old', 'images': ['/tmp/old.png']}]
        self.panel.images = [f'/tmp/{i}.png' for i in range(4)]
        self.panel.prompt.setPlainText('new')
        self.panel._update_failed_drafts()
        self.panel.restore_draft.click()
        self.assertEqual(self.panel.prompt.toPlainText(), 'new')
        self.assertEqual(len(self.panel.images), 4)
        self.assertEqual(len(self.panel.failed_drafts), 1)
        self.assertIn('超過上限', self.panel.status.text())

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
        self.assertIn('[圖片]', self.panel.transcript.toPlainText())
        self.assertNotIn('private-snapshot', self.panel.transcript.toPlainText())

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
        self.assertIn('選擇本次執行權限', self.panel.status.text())
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
        for fragment in ('開始：命令', '第一步', '檢查', 'a → b', '服務調整'):
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
        self.assertIn('選擇本次執行權限', self.panel.status.text())

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
