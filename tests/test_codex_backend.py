import json
import urllib.parse
import io
from types import SimpleNamespace
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import codex_backend as backend
from codex_backend_auth import ChatGPTAuth


class FakeAuth:
    def token(self): return 'test-oauth-token'
    def account(self): return {'source': 'ChatGPT plan', 'signedIn': True}


class Response:
    def __init__(self, obj=None, lines=()): self.obj, self.lines = obj or {}, lines
    def raise_for_status(self): pass
    def json(self): return self.obj
    def iter_lines(self, decode_unicode=False): return iter(self.lines)
    def close(self): pass


class BackendTests(unittest.TestCase):
    @staticmethod
    def image_bytes():
        data = io.BytesIO()
        Image.new('RGB', (8, 8), (220, 30, 40)).save(data, 'PNG')
        return data.getvalue()

    def test_images_validate_user_selected_files_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'red.png'
            path.write_bytes(self.image_bytes())
            loaded = backend.load_images([path])
            self.assertEqual(loaded[0][:2], ('image/png', '.png'))
            self.assertEqual(loaded[0][2], path.read_bytes())
            with self.assertRaisesRegex(ValueError, '最多'):
                backend.load_images([path] * 5)
            with mock.patch.object(backend, 'MAX_IMAGE_BYTES', 10):
                with self.assertRaisesRegex(ValueError, '10 MiB'):
                    backend.load_images([path])
            with mock.patch.object(backend, 'MAX_IMAGE_TOTAL_BYTES', len(loaded[0][2]) + 1):
                with self.assertRaisesRegex(ValueError, '总大小'):
                    backend.load_images([path, path])
            invalid = Path(directory) / 'bad.png'
            invalid.write_bytes(b'not a valid picture')
            with self.assertRaisesRegex(ValueError, '图片无效'):
                backend.load_images([invalid])
            animated = Path(directory) / 'animated.gif'
            first = Image.new('RGB', (2, 2), 'red')
            first.save(animated, save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')], duration=10)
            with self.assertRaisesRegex(ValueError, '动态图'):
                backend.load_images([animated])

    def test_chat_image_payload_history_and_model_rejection(self):
        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        data = self.image_bytes()
        images = [('image/png', '.png', data)]
        catalog = Response({'models': [{'slug': 'vision', 'visibility': 'list', 'input_modalities': ['text', 'image']}]})
        stream = Response(lines=['data: ' + json.dumps({'type': 'response.completed'})])
        with mock.patch.object(backend.requests, 'get', return_value=catalog), mock.patch.object(backend.requests, 'post', return_value=stream) as post:
            b._chat('what color?', 'vision', images)
            content = post.call_args.kwargs['json']['input'][0]['content']
            self.assertEqual(content[0], {'type': 'input_text', 'text': 'what color?'})
            self.assertTrue(content[1]['image_url'].startswith('data:image/png;base64,'))
            self.assertEqual(b.history_image_bytes, len(data))
            with mock.patch.object(backend, 'MAX_CHAT_IMAGE_BYTES', len(data)):
                with self.assertRaisesRegex(RuntimeError, '新对话'):
                    b._chat('again', 'vision', images)
        b.new_thread('chat', None)
        self.assertEqual(b.history_image_bytes, 0)
        text_only = Response({'models': [{'slug': 'text', 'visibility': 'list', 'input_modalities': ['text']}]})
        with mock.patch.object(backend.requests, 'get', return_value=text_only), mock.patch.object(backend.requests, 'post') as post:
            with self.assertRaisesRegex(RuntimeError, '不支持图片'):
                b._chat('what color?', 'text', images)
            post.assert_not_called()

    def test_agent_images_are_private_snapshots_and_removed_on_success_and_failure(self):
        image_data = self.image_bytes()
        class Server:
            fail = False
            last = None
            def __init__(self, *args, **kwargs):
                self.completed_turns = {}
                self.snapshot = None
                Server.last = self
            def request(self, method, params):
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method in ('thread/start', 'thread/resume'): return {'thread': {'id': 't'}, 'model': 'm'}
                if method == 'model/list': return {'data': [{'model': 'm', 'inputModalities': ['text', 'image']}]}
                if method == 'turn/start':
                    self.snapshot = Path(params['input'][1]['path'])
                    if not self.snapshot.is_file() or self.snapshot.read_bytes() != image_data:
                        raise AssertionError('snapshot not readable at turn/start')
                    if self.snapshot.stat().st_mode & 0o777 != 0o600:
                        raise AssertionError('snapshot is not private')
                    if self.fail: raise RuntimeError('simulated turn failure')
                    self.completed_turns['u'] = {'status': 'completed'}
                    return {'turn': {'id': 'u'}}
                raise AssertionError(method)
            def close(self): pass
        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        images = [('image/png', '.png', image_data)]
        catalog = Response({'models': [{'slug': 'm', 'visibility': 'list', 'input_modalities': ['image']}]})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, 'AppServer', Server), mock.patch.object(backend.requests, 'get', return_value=catalog):
            for mode in ('plan', 'task'):
                self.assertEqual(b._agent(mode, 'look', directory, model='m', images=images), 'completed')
                self.assertFalse(Server.last.snapshot.exists())
            Server.fail = True
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                b._agent('task', 'look', directory, model='m', images=images)
            self.assertFalse(Server.last.snapshot.exists())

    def test_login_completion_and_model_generations_are_reported(self):
        events, ready = [], threading.Event()
        def emit(event, data):
            events.append((event, data))
            if event in ('login_complete', 'models', 'models_error'):
                ready.set()
        auth = FakeAuth()
        auth.login = lambda: None
        b = backend.Backend(emit, auth=auth)
        b.login()
        self.assertTrue(ready.wait(2))
        self.assertEqual(events[-1], ('login_complete', None))
        ready.clear()
        catalog = Response({'models': [{'slug': 'one', 'visibility': 'list'}]})
        with mock.patch.object(backend.requests, 'get', return_value=catalog):
            b.models('chat', generation=42)
            self.assertTrue(ready.wait(2))
        self.assertEqual(events[-1][1]['generation'], 42)
        ready.clear()
        b.models('invalid', generation=43)
        self.assertTrue(ready.wait(2))
        self.assertEqual(events[-1][0], 'models_error')
        self.assertEqual(events[-1][1]['generation'], 43)

    def test_model_catalogs_are_mode_specific_and_selected_model_is_used(self):
        events, ready = [], threading.Event()
        def emit(event, data):
            events.append((event, data))
            if event in ('models', 'models_error'):
                ready.set()
        b = backend.Backend(emit, auth=FakeAuth())
        catalog = Response({'models': [
            {'slug': 'one', 'display_name': 'One', 'visibility': 'list'},
            {'slug': 'two', 'display_name': 'Two', 'visibility': 'list'},
            {'slug': 'hidden', 'visibility': 'hide'}]})
        stream = Response(lines=['data: ' + json.dumps({'type': 'response.completed'})])
        with mock.patch.object(backend.requests, 'get', return_value=catalog), mock.patch.object(backend.requests, 'post', return_value=stream) as post:
            b.models('chat')
            self.assertTrue(ready.wait(2))
            self.assertEqual([item['id'] for item in events[-1][1]['models']], ['one', 'two'])
            b._chat('hi', 'two')
            self.assertEqual(post.call_args.kwargs['json']['model'], 'two')
            with self.assertRaisesRegex(RuntimeError, '刷新模型'):
                b._chat('hi', 'hidden')

        calls = []
        class Server:
            def __init__(self, *args, **kwargs): pass
            def request(self, method, params):
                calls.append((method, params))
                if method == 'account/read':
                    return {'account': {'type': 'chatgpt'}}
                return {'data': [{'model': 'codex', 'displayName': 'Codex',
                                  'supportedReasoningEfforts': [{'reasoningEffort': 'high'}],
                                  'defaultReasoningEffort': 'high', 'isDefault': True}]}
            def close(self): pass
        ready.clear()
        with mock.patch.object(backend, 'AppServer', Server):
            b.models('task')
            self.assertTrue(ready.wait(2))
        self.assertEqual(events[-1][1]['models'][0]['efforts'], ['high'])
        self.assertEqual([method for method, _ in calls], ['account/read', 'model/list'])

    def test_codex_history_requires_selection_and_rejects_cross_directory(self):
        events, ready, requests_seen = [], threading.Event(), []
        def emit(event, data):
            events.append((event, data))
            if event in ('codex_threads', 'codex_threads_error', 'codex_thread', 'codex_thread_error'):
                ready.set()
        class Server:
            def __init__(self, *args, **kwargs): self.mode = None
            def request(self, method, params):
                requests_seen.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/list': return {'data': [{'id': 'old'}], 'nextCursor': None}
                if method == 'thread/read': return {'thread': {'id': 'old', 'cwd': '/not-the-project', 'turns': []}}
                raise AssertionError('must not resume an unrelated session')
            def close(self): pass
        b = backend.Backend(emit, auth=FakeAuth())
        with mock.patch.object(backend, 'AppServer', Server), tempfile.TemporaryDirectory() as directory:
            b.codex_threads()
            self.assertTrue(ready.wait(2))
            self.assertNotIn('thread/read', [method for method, _ in requests_seen])
            ready.clear()
            b.codex_thread('old')
            self.assertTrue(ready.wait(2))
            self.assertEqual(requests_seen[-1], ('thread/read', {'threadId': 'old', 'includeTurns': True}))
            with self.assertRaisesRegex(RuntimeError, '工作目录'):
                b._agent('task', 'do something', directory, thread_id='old', task_sandbox='read-only')
            self.assertNotIn('thread/resume', [method for method, _ in requests_seen])
        b.send('chat', 'hi', None, thread_id='old')
        self.assertIn('只能在任务模式', events[-1][1])

    def test_selected_codex_session_resumes_with_selected_model_and_effort(self):
        class Server:
            last = None
            def __init__(self, *args, **kwargs):
                self.requests, self.completed_turns = [], {}
                Server.last = self
            def request(self, method, params):
                self.requests.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/read': return {'thread': {'id': 'selected', 'cwd': directory, 'modelProvider': 'openai'}}
                if method == 'thread/resume':
                    return {'thread': {'id': 'selected'}, 'sandbox': {'type': 'readOnly'}, 'approvalPolicy': 'on-request'}
                if method == 'turn/start':
                    self.completed_turns['turn'] = {'id': 'turn', 'status': 'completed'}
                    return {'turn': {'id': 'turn'}}
                raise AssertionError(method)
            def close(self): pass
        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, 'AppServer', Server):
            self.assertEqual(b._agent('task', 'hello', directory, 'selected-model', 'high', 'selected', 'read-only'), 'completed')
        requests = dict(Server.last.requests)
        self.assertEqual(requests['thread/resume']['threadId'], 'selected')
        self.assertEqual(requests['thread/resume']['sandbox'], 'read-only')
        self.assertEqual(requests['turn/start']['model'], 'selected-model')
        self.assertEqual(requests['turn/start']['effort'], 'high')
        self.assertEqual(b.threads[('task', directory)], 'selected')

    def test_history_resume_requires_explicit_permission_and_confirmation(self):
        calls = []
        class Server:
            def __init__(self, *args, **kwargs): self.mode = None
            def request(self, method, params):
                calls.append(method)
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/read': return {'thread': {'id': 'old', 'cwd': directory, 'modelProvider': 'openai', 'model': 'm', 'reasoningEffort': 'low'}}
                if method == 'thread/resume':
                    return {'thread': {'id': 'old'}, 'sandbox': {'type': 'workspaceWrite'}, 'approvalPolicy': 'on-request'}
                raise AssertionError('must not start when selected permission was not confirmed')
            def close(self): pass
        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, 'AppServer', Server):
            with self.assertRaisesRegex(RuntimeError, '明确选择'):
                b._agent('task', 'continue', directory, thread_id='old')
            self.assertEqual(calls, [])
            with self.assertRaisesRegex(RuntimeError, '未确认'):
                b._agent('task', 'continue', directory, thread_id='old', task_sandbox='read-only')
            self.assertNotIn('turn/start', calls)

    def test_history_resume_does_not_silently_switch_model_or_effort(self):
        class Server:
            result_model, result_effort = 'old-model', 'low'
            def __init__(self, *args, **kwargs):
                self.completed_turns = {}
                self.requests = []
                Server.last = self
            def request(self, method, params):
                self.requests.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/read':
                    return {'thread': {'id': 'old', 'cwd': directory, 'modelProvider': 'openai',
                                       'model': 'old-model', 'reasoningEffort': 'low'}}
                if method == 'thread/resume':
                    return {'thread': {'id': 'old'}, 'sandbox': {'type': 'readOnly'},
                            'approvalPolicy': 'on-request', 'model': self.result_model,
                            'reasoningEffort': self.result_effort}
                if method == 'turn/start':
                    self.completed_turns['t'] = {'id': 't', 'status': 'completed'}
                    return {'turn': {'id': 't'}}
                raise AssertionError(method)
            def close(self): pass
        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, 'AppServer', Server):
            self.assertEqual(b._agent('task', 'continue', directory, thread_id='old', task_sandbox='read-only'), 'completed')
            turn_params = dict(Server.last.requests)['turn/start']
            self.assertNotIn('model', turn_params)
            self.assertNotIn('effort', turn_params)
            Server.result_model = 'other-model'
            with self.assertRaisesRegex(RuntimeError, '模型与历史模型不一致'):
                b._agent('task', 'continue', directory, thread_id='old', task_sandbox='read-only')
            Server.result_model, Server.result_effort = 'old-model', 'high'
            with self.assertRaisesRegex(RuntimeError, '推理强度与历史不一致'):
                b._agent('task', 'continue', directory, thread_id='old', task_sandbox='read-only')
            self.assertNotIn('turn/start', dict(Server.last.requests))

    def test_quota_failure_is_tagged_and_close_failure_is_not_success(self):
        events, finished = [], threading.Event()
        def emit(event, data):
            events.append((event, data))
            if event in ('limits', 'limits_error'):
                finished.set()
        class Server:
            def __init__(self, emit, executable=None): pass
            def request(self, method, params):
                if method == 'account/read':
                    return {'account': {'type': 'chatgpt'}}
                return {'rateLimits': {'primary': {'usedPercent': 25}}}
            def close(self):
                raise RuntimeError('无法关闭额度连接')
        b = backend.Backend(emit, auth=FakeAuth())
        with mock.patch.object(backend, 'AppServer', Server):
            b.limits()
            self.assertTrue(finished.wait(3))
        self.assertEqual(events, [('limits_error', '无法关闭额度连接')])

    def test_chat_uses_oauth_and_requires_completed(self):
        events = []
        b = backend.Backend(lambda *x: events.append(x), auth=FakeAuth())
        models = Response({'models': [{'slug': 'model-one', 'visibility': 'list'}]})
        stream = Response(lines=['data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': 'hello'}),
                                 'data: ' + json.dumps({'type': 'response.completed'})])
        with mock.patch.object(backend.requests, 'get', return_value=models), mock.patch.object(backend.requests, 'post', return_value=stream) as post:
            b._chat('hi')
        self.assertEqual(events, [('delta', 'hello')])
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-oauth-token')
        self.assertFalse(kwargs['json']['store'])
        self.assertTrue(kwargs['json']['stream'])
        self.assertNotIn('tools', kwargs['json'])
        self.assertEqual(b.history[-1], {'role': 'assistant', 'content': 'hello'})
        with mock.patch.object(backend.requests, 'get', return_value=models), mock.patch.object(backend.requests, 'post', return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, '完整结束'):
                b._chat('again')
        self.assertEqual(len(b.history), 2)
        # Requests can yield raw bytes for text/event-stream without a charset.
        byte_stream = Response(lines=[('data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': '你好'}, ensure_ascii=False)).encode('utf-8'),
                                      ('data: ' + json.dumps({'type': 'response.completed'})).encode('utf-8')])
        with mock.patch.object(backend.requests, 'get', return_value=models), mock.patch.object(backend.requests, 'post', return_value=byte_stream):
            b._chat('bytes')
        self.assertEqual(b.history[-1]['content'], '你好')

    def test_approvals_are_not_silently_granted(self):
        sent, events = [], []
        server = object.__new__(backend.AppServer)
        server.emit = lambda *x: events.append(x)
        server.write = sent.append
        server.pending_approvals = {}
        server.completed_turns = {}
        server.mode = 'plan'
        server.handle({'method': 'turn/completed', 'params': {'turn': {'id': 'early', 'status': 'completed'}}})
        self.assertEqual(server.completed_turns['early']['status'], 'completed')
        server.handle({'id': 8, 'method': 'item/permissions/requestApproval', 'params': {'permissions': {'network': {'enabled': True}}}})
        self.assertEqual(sent[-1]['result'], {'permissions': {}})
        server.mode = 'task'
        params = {'permissions': {'network': {'enabled': True}}, 'reason': 'network required'}
        server.handle({'id': 9, 'method': 'item/permissions/requestApproval', 'params': params})
        self.assertEqual(events[-1][0], 'approval')
        server.approve(9, 'decline')
        self.assertEqual(sent[-1]['result'], {'permissions': {}, 'scope': 'turn'})
        server.handle({'id': 10, 'method': 'item/tool/requestUserInput', 'params': {'questions': [{'id': 'q'}]}})
        server.approve(10, {'answers': {'q': {'answers': ['option']}}})
        self.assertEqual(sent[-1]['result']['answers']['q']['answers'], ['option'])
        server.handle({'id': 11, 'method': 'unknown/request', 'params': {}})
        self.assertIn('error', sent[-1])

    def test_progress_keeps_plan_and_tool_lifecycle_without_private_outputs(self):
        events = []
        server = object.__new__(backend.AppServer)
        server.emit = lambda *x: events.append(x)
        server.handle({'method': 'turn/plan/updated', 'params': {
            'plan': [{'step': 'Inspect project', 'status': 'inProgress'}]}})
        server.handle({'method': 'item/plan/delta', 'params': {'itemId': 'p', 'delta': 'Plan'}})
        server.handle({'method': 'item/completed', 'params': {'item': {
            'type': 'plan', 'id': 'p', 'text': 'Final plan'}}})
        server.handle({'method': 'item/started', 'params': {'item': {
            'id': 'c', 'type': 'commandExecution', 'command': 'SECRET',
            'aggregatedOutput': 'PRIVATE', 'status': 'inProgress'}}})
        server.handle({'method': 'item/completed', 'params': {'item': {
            'id': 'f', 'type': 'fileChange', 'changes': [{'path': 'app.py', 'diff': 'SECRET'}],
            'status': 'completed'}}})
        server.handle({'method': 'model/rerouted', 'params': {
            'fromModel': 'a', 'toModel': 'b', 'reason': 'load'}})
        self.assertEqual(events[0][1]['steps'][0]['step'], 'Inspect project')
        self.assertEqual(events[1][1]['kind'], 'plan_delta')
        self.assertEqual(events[2][1]['text'], 'Final plan')
        self.assertEqual(events[3][1]['phase'], 'started')
        self.assertEqual(events[4][1]['detail'], 'app.py')
        self.assertEqual(events[5][1]['to_model'], 'b')
        self.assertNotIn('SECRET', repr(events))
        self.assertNotIn('PRIVATE', repr(events))

    def test_task_rejects_api_key_before_thread_start(self):
        class Server:
            def __init__(self, *args, **kwargs): self.mode = None
            def request(self, method, params):
                self.method = method
                if method == 'account/read': return {'account': {'type': 'apiKey'}}
                raise AssertionError('task must not start')
            def close(self): pass
        b = backend.Backend(lambda *x: None, auth=FakeAuth())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, 'AppServer', Server):
            with self.assertRaisesRegex(RuntimeError, 'API key'):
                b._agent('task', 'test', directory)

    def test_plan_mode_sets_read_only_and_uses_oauth(self):
        class Server:
            last = None
            final_status = 'completed'
            def __init__(self, emit, token, executable):
                self.token, self.requests, self.mode, self.completed_turns = token, [], None, {}
                Server.last = self
            def request(self, method, params):
                self.requests.append((method, params))
                if method in ('thread/start', 'thread/resume'): return {'thread': {'id': 'thread'}, 'model': 'm'}
                if method == 'turn/start': return {'turn': {'id': 'turn'}}
            def next_event(self): return {'method': 'turn/completed', 'params': {'turn': {'id': 'turn', 'status': self.final_status}}}
            def handle(self, event):
                self.completed_turns[event['params']['turn']['id']] = event['params']['turn']
                return event['method']
            def close(self): pass
        b = backend.Backend(lambda *x: None, auth=FakeAuth())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, 'AppServer', Server), mock.patch.object(backend.requests, 'get', return_value=Response({'models': [{'slug': 'm', 'visibility': 'list'}]})):
            self.assertEqual(b._agent('plan', 'test', directory), 'completed')
            Server.final_status = 'interrupted'
            self.assertEqual(b._agent('plan', 'test', directory), 'interrupted')
        server = Server.last
        self.assertEqual(server.token, 'test-oauth-token')
        self.assertEqual(server.requests[0][1]['sandbox'], 'read-only')
        turn = server.requests[1][1]
        self.assertEqual(turn['collaborationMode']['mode'], 'plan')
        self.assertEqual(turn['sandboxPolicy']['type'], 'readOnly')
        self.assertEqual(turn['approvalPolicy'], 'never')

    def test_server_interruption_is_reported_as_interrupted(self):
        events, finished = [], threading.Event()
        b = backend.Backend(lambda *x: (events.append(x), finished.set() if x[0] == 'done' else None), auth=FakeAuth())
        with mock.patch.object(b, '_agent', return_value='interrupted'):
            b.send('task', 'test', '/tmp')
            self.assertTrue(finished.wait(2))
        self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'interrupted'}))
        self.assertFalse(any(event[0] == 'error' for event in events))

    def test_limits_rejects_missing_codex_login(self):
        finished = threading.Event()
        events = []
        class Server:
            def __init__(self, *args, **kwargs): pass
            def request(self, method, params):
                self.assert_only_account_read(method)
                return {'account': None}
            def assert_only_account_read(self, method):
                if method != 'account/read': raise AssertionError('no limit read without login')
            def close(self): finished.set()
        b = backend.Backend(lambda *x: events.append(x), auth=FakeAuth())
        with mock.patch.object(backend, 'AppServer', Server):
            b.limits()
            self.assertTrue(finished.wait(2))
            for _ in range(100):
                if events: break
                time.sleep(0.005)
        self.assertEqual(events[-1][0], 'limits_error')
        self.assertIn('不会使用 API key', events[-1][1])

    def test_account_mismatch_fails_and_missing_email_is_unverified(self):
        events = []
        auth = mock.Mock()
        auth.account.return_value = {'signedIn': True, 'email': 'one@example.invalid'}
        b = backend.Backend(lambda *event: events.append(event), auth=auth)
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            b._check_identity({'type': 'chatgpt', 'email': 'two@example.invalid'})
        b._check_identity({'type': 'chatgpt', 'email': None})
        self.assertIn('无法确认', events[-1][1])
        b._check_identity({'type': 'chatgpt', 'email': 'ONE@example.invalid'})
        self.assertIn('未验证', events[-1][1])

    def test_discover_newest_valid_vscode_bundle(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend.shutil, 'which', return_value=None), \
             mock.patch.object(backend.platform, 'machine', return_value='x86_64'):
            root = Path(directory) / '.vscode' / 'extensions'
            for version, publisher in [('1.3.0', 'openai'), ('1.12.0', 'openai'), ('9.0.0', 'fake')]:
                ext = root / ('openai.chatgpt-' + version)
                cli = ext / 'bin' / 'linux-x86_64' / 'codex'
                cli.parent.mkdir(parents=True)
                cli.write_text('test')
                cli.chmod(0o755)
                (ext / 'package.json').write_text(json.dumps({'publisher': publisher, 'name': 'chatgpt', 'version': version}))
            self.assertIn('1.12.0', backend.discover_codex(directory))

    def test_oauth_checks_state_signature_and_stores_private_credentials(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        captured = {}
        def browser(url):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            captured.update({k: v[0] for k, v in params.items()})
            handler = fake_server.handler.__new__(fake_server.handler)
            handler.path = '/auth/callback?' + urllib.parse.urlencode({
                'state': params['state'][0], 'code': 'test-code', 'client_id': 'issued-test'})
            handler.send_response = lambda *args: None
            handler.send_header = lambda *args: None
            handler.end_headers = lambda: None
            handler.wfile = io.BytesIO()
            handler.do_GET()
            return True
        def token_post(url, data, timeout):
            self.assertEqual(data['client_id'], 'issued-test')
            self.assertEqual(data['redirect_uri'], captured['redirect_uri'])
            claims = {'iss': 'https://auth.openai.com', 'aud': 'issued-test',
                      'sub': 'test-subject', 'nonce': captured['nonce'], 'exp': int(time.time()) + 600}
            token = jwt.encode(claims, key, algorithm='RS256')
            return Response({'id_token': token, 'access_token': 'test-access', 'refresh_token': 'test-refresh',
                             'expires_in': 3600, 'scope': 'chatgpt.tokens.use.direct'})
        class FakeServer:
            def __init__(self, address, handler):
                nonlocal fake_server
                fake_server = self
                self.handler = handler
                self.server_address = ('127.0.0.1', 12345)
                self.stopped = threading.Event()
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def serve_forever(self): self.stopped.wait()
            def shutdown(self): self.stopped.set()
        fake_server = None
        jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch('codex_backend_auth.socketserver.ThreadingTCPServer', FakeServer), \
             mock.patch('codex_backend_auth.webbrowser.open', side_effect=browser), \
             mock.patch('codex_backend_auth.requests.post', side_effect=token_post), \
             mock.patch('codex_backend_auth.jwt.PyJWKClient', return_value=jwks):
            auth = ChatGPTAuth(directory)
            auth.login()
            self.assertEqual(auth.token(), 'test-access')
            self.assertEqual(auth.path.stat().st_mode & 0o777, 0o600)
            record = json.loads(auth.path.read_text())
            self.assertEqual(record['subject'], 'test-subject')
            self.assertEqual(record['client_id'], 'issued-test')
            self.assertEqual(captured['client_id'], 'dynamic_agent_client')
            self.assertEqual(captured['code_challenge_method'], 'S256')

    def test_credentials_saved_owner_only_and_not_marked_signed_in_without_file(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = ChatGPTAuth(directory)
            self.assertFalse(auth.account()['signedIn'])
            auth._write(auth.path, json.dumps({'email': 'test@example.invalid'}))
            self.assertEqual(auth.path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(auth.account()['signedIn'])
            self.assertEqual(auth._host(), auth._host())


if __name__ == '__main__': unittest.main()
