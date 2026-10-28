import io
import json
import os
from pathlib import Path
import queue
import sys
import threading
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PyQt5.QtCore import QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication, QInputDialog, QPushButton

from codex_backend import AppServer, Backend
from pet_panel import PetPanel


def server_for_test():
    server = object.__new__(AppServer)
    server.emit = mock.Mock()
    server.write = mock.Mock()
    server.pending_approvals = {}
    server.pending_command_snapshots = {}
    server.pending_patch_snapshots = {}
    server.command_items = {}
    server.file_change_patches = {}
    server.completed_turns = {}
    server.queued_server_requests = set()
    server.resolved_queued_requests = set()
    server.event_lock = threading.RLock()
    server.events = queue.Queue()
    server.mode = 'task'
    return server


class ApprovalRaceTests(unittest.TestCase):
    def test_read_thread_resolves_request_before_consumer_sees_it(self):
        server = server_for_test()
        request = {'id': 7, 'method': 'item/permissions/requestApproval',
                   'params': {'threadId': 'a', 'turnId': 't', 'permissions': {}}}
        resolved = {'method': 'serverRequest/resolved',
                    'params': {'threadId': 'a', 'requestId': 7}}
        server.process = mock.Mock(stdout=io.StringIO('\n'.join(map(json.dumps, (request, resolved))) + '\n'))
        reader = threading.Thread(target=server._read)
        reader.start()
        reader.join(2)
        self.assertFalse(reader.is_alive())
        server.handle(server.events.get_nowait())
        server.emit.assert_not_called()
        self.assertFalse(server.pending_approvals)
        self.assertFalse(server.resolved_queued_requests)

    def test_resolve_is_scoped_by_thread_and_request_and_blocks_late_accept(self):
        server = server_for_test()
        params = {'threadId': 'a', 'turnId': 'turn', 'permissions': {'network': {'enabled': True}}}
        server.handle({'id': 7, 'method': 'item/permissions/requestApproval', 'params': params})
        server.handle({'method': 'serverRequest/resolved', 'params': {'threadId': 'other', 'requestId': 7}})
        self.assertIn(7, server.pending_approvals)
        server.handle({'method': 'serverRequest/resolved', 'params': {'threadId': 'a', 'requestId': 7}})
        self.assertNotIn(7, server.pending_approvals)
        with self.assertRaisesRegex(RuntimeError, '已失效'):
            server.approve(7, 'accept')
        server.write.assert_not_called()
        self.assertEqual(server.emit.call_args.args, ('approval_resolved', {'id': 7, 'threadId': 'a'}))

    def test_turn_completion_and_cancel_invalidate_only_affected_requests(self):
        server = server_for_test()
        for ident, thread, turn in ((1, 'a', 'one'), (2, 'a', 'two'), (3, 'b', 'one')):
            server.handle({'id': ident, 'method': 'item/permissions/requestApproval',
                           'params': {'threadId': thread, 'turnId': turn}})
        server.handle({'method': 'turn/completed', 'params': {'threadId': 'a', 'turn': {'id': 'one'}}})
        self.assertEqual(set(server.pending_approvals), {2, 3})
        server.invalidate_approvals()
        self.assertFalse(server.pending_approvals)
        self.assertEqual(server.emit.call_count, 6)

    def test_backend_cancel_invalidates_and_wakes_server_without_reply(self):
        events = []
        backend = Backend(lambda *args: events.append(args), auth=object())
        server = server_for_test()
        server.emit = backend.emit
        server.stop_event = backend.cancelled
        backend.active = server
        server.handle({'id': 4, 'method': 'item/permissions/requestApproval',
                       'params': {'threadId': 'a', 'turnId': 't', 'permissions': {}}})
        backend.cancel()
        self.assertTrue(backend.cancelled.is_set())
        self.assertFalse(server.pending_approvals)
        self.assertEqual(events[-1], ('approval_resolved', {'id': 4, 'threadId': 'a'}))
        backend.approve(4, 'accept')
        server.write.assert_not_called()
        self.assertEqual(server.events.get_nowait(), {'_pet_wake': True})

    def test_old_server_cannot_check_or_approve_new_servers_same_request_id(self):
        events = []
        backend = Backend(lambda *args: events.append(args), auth=object())
        old, new = server_for_test(), server_for_test()
        old.server_id, new.server_id = 'old', 'new'
        request = {'id': 4, 'method': 'item/permissions/requestApproval',
                   'params': {'threadId': 'same', 'turnId': 't', 'permissions': {}}}
        old.handle(request)
        new.handle(request)
        backend.active = old
        self.assertTrue(backend.approval_is_pending(4, 'same', 'old'))
        backend.active = new
        self.assertFalse(backend.approval_is_pending(4, 'same', 'old'))
        self.assertTrue(backend.approval_is_pending(4, 'same', 'new'))
        backend.approve(4, 'accept', 'old')
        old.write.assert_not_called()
        new.write.assert_not_called()
        self.assertIn(4, new.pending_approvals)
        backend.approve(4, 'decline', 'new')
        self.assertNotIn(4, new.pending_approvals)
        self.assertEqual(new.write.call_count, 1)

    def test_live_plan_and_unknown_requests_get_refusal_but_closed_reader_never_writes(self):
        server = server_for_test()
        server.mode = 'plan'
        plan = {'id': 2, 'method': 'item/permissions/requestApproval',
                'params': {'threadId': 'a', 'permissions': {'network': {'enabled': True}}}}
        unknown = {'id': 3, 'method': 'unsupported/request', 'params': {'threadId': 'a'}}
        server.handle(plan)
        server.handle(unknown)
        self.assertEqual(server.write.call_args_list[0].args[0]['result'], {'permissions': {}})
        self.assertIn('error', server.write.call_args_list[1].args[0])
        server.reader_closed = True
        server.write.reset_mock()
        server.handle(plan)
        server.handle(unknown)
        server.write.assert_not_called()


class FakeBackend:
    def __init__(self, emit):
        self.emit, self.pending, self.calls = emit, True, []
    def approval_is_pending(self, ident, thread): return self.pending
    def approve(self, *args): self.calls.append(args)
    def close(self): pass


class ApprovalDialogRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = PetPanel(backend_factory=FakeBackend)

    def tearDown(self):
        self.panel.hide()
        self.panel.deleteLater()
        self.app.processEvents()

    def test_queued_approval_never_shown_if_already_invalid(self):
        self.panel.backend.pending = False
        self.panel._approval({'id': 7, 'method': 'item/permissions/requestApproval',
                              'params': {'threadId': 'a', 'permissions': {}}})
        self.assertIsNone(QApplication.activeModalWidget())
        self.assertEqual(self.panel.backend.calls, [])

    def test_background_resolution_closes_command_dialog_without_sending_decision(self):
        def resolve():
            dialog = QApplication.activeModalWidget()
            self.assertTrue(any(button.text() == '允许' for button in dialog.findChildren(QPushButton)))
            thread = threading.Thread(target=lambda: self.panel.bridge.safe_emit(
                'approval_resolved', {'id': 7, 'threadId': 'a'}))
            thread.start()
            thread.join(2)
        QTimer.singleShot(0, resolve)
        QTimer.singleShot(1000, lambda: QApplication.activeModalWidget() and QApplication.activeModalWidget().reject())
        self.panel._approval({'id': 7, 'method': 'item/permissions/requestApproval',
                              'params': {'threadId': 'a', 'permissions': {}}})
        self.assertEqual(self.panel.backend.calls, [])
        self.assertEqual(self.panel.status.text(), '审批请求已失效。')

    def test_resolution_closes_question_dialog_without_submitting_cancel(self):
        observed = []
        def resolve():
            dialog = QApplication.activeModalWidget()
            observed.append(isinstance(dialog, QInputDialog))
            self.panel.bridge.safe_emit('approval_resolved', {'id': 8, 'threadId': 'a'})
            observed.append(dialog.isVisible())
        QTimer.singleShot(0, resolve)
        QTimer.singleShot(1000, lambda: QApplication.activeModalWidget() and QApplication.activeModalWidget().reject())
        self.panel._approval({'id': 8, 'method': 'item/tool/requestUserInput',
                              'params': {'threadId': 'a', 'questions': [{'id': 'q', 'question': '测试'}]}})
        self.assertEqual(observed, [True, False])
        self.assertEqual(self.panel.backend.calls, [])

    def test_worker_to_qt_modal_resolution_closes_without_reply(self):
        server = server_for_test()
        server.emit = self.panel.bridge.safe_emit
        self.panel.backend.approval_is_pending = lambda ident, thread: (
            ident in server.pending_approvals and server.pending_approvals[ident][1]['threadId'] == thread)
        self.panel.backend.approve = server.approve
        request = {'id': 17, 'method': 'item/permissions/requestApproval',
                   'params': {'threadId': 'a', 'turnId': 't', 'permissions': {}}}
        observations = []
        loop = QEventLoop()
        def resolve():
            observations.append(QApplication.activeModalWidget() is not None)
            worker = threading.Thread(target=server.handle, args=({'method': 'serverRequest/resolved',
                'params': {'threadId': 'a', 'requestId': 17}},))
            worker.start()
            worker.join(2)
        def start():
            worker = threading.Thread(target=server.handle, args=(request,))
            worker.start()
            worker.join(2)
            QTimer.singleShot(30, resolve)
            QTimer.singleShot(80, loop.quit)
        QTimer.singleShot(0, start)
        QTimer.singleShot(1000, loop.quit)
        loop.exec_()
        self.assertEqual(observations, [True])
        self.assertNotIn(17, server.pending_approvals)
        server.write.assert_not_called()
        self.assertEqual(self.panel.approval_contexts, [])

    def test_old_server_resolution_does_not_close_new_server_dialog(self):
        self.panel.backend.approval_is_pending = lambda ident, thread, server: True
        observed = []
        def resolve_old():
            dialog = QApplication.activeModalWidget()
            self.panel.bridge.safe_emit('approval_resolved', {'id': 7, 'threadId': 'a', 'serverId': 'old'})
            observed.append(dialog.isVisible())
            self.panel.bridge.safe_emit('approval_resolved', {'id': 7, 'threadId': 'a', 'serverId': 'new'})
            observed.append(dialog.isVisible())
        QTimer.singleShot(0, resolve_old)
        QTimer.singleShot(1000, lambda: QApplication.activeModalWidget() and QApplication.activeModalWidget().reject())
        self.panel._approval({'id': 7, 'serverId': 'new', 'method': 'item/permissions/requestApproval',
                              'params': {'threadId': 'a', 'permissions': {}}})
        self.assertEqual(observed, [True, False])
        self.assertEqual(self.panel.backend.calls, [])

    def test_old_dialog_accept_after_server_switch_never_approves_new_request(self):
        backend = Backend(self.panel.bridge.safe_emit, auth=object())
        self.panel.backend = backend
        old, new = server_for_test(), server_for_test()
        old.server_id, new.server_id = 'old', 'new'
        params = {'threadId': 'a', 'turnId': 't', 'permissions': {'network': {'enabled': True}}}
        request = {'id': 7, 'method': 'item/permissions/requestApproval', 'params': params}
        old.handle(request)
        new.handle(request)
        backend.active = old
        clicks = []
        def switch_and_accept():
            backend.active = new
            dialog = QApplication.activeModalWidget()
            buttons = [button for button in dialog.findChildren(QPushButton) if button.text() == '允许']
            clicks.append(len(buttons))
            if buttons:
                buttons[0].click()
        QTimer.singleShot(0, switch_and_accept)
        QTimer.singleShot(1000, lambda: QApplication.activeModalWidget() and QApplication.activeModalWidget().reject())
        self.panel._approval(dict(request, serverId='old'))
        self.assertEqual(clicks, [1])
        old.write.assert_not_called()
        new.write.assert_not_called()
        self.assertIn(7, new.pending_approvals)
