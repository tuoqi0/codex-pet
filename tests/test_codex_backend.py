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
    def _stop_protocol(self, scenario):
        """Exercise JSON-RPC over real pipes, including a pending approval."""
        program = '''#!/usr/bin/python3
import json, sys, time
scenario = %r
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method, ident = message.get('method'), message.get('id')
    if method == 'initialize':
        emit({'id': ident, 'result': {}})
    elif method == 'account/read':
        emit({'id': ident, 'result': {'account': {'type': 'chatgpt'}}})
    elif method in ('thread/start', 'thread/resume'):
        emit({'id': ident, 'result': {'thread': {'id': 'test-thread'}, 'model': 'test-model'}})
    elif method == 'model/list':
        emit({'id': ident, 'result': {'data': [{'model': 'test-model', 'inputModalities': ['text', 'image']}]}})
    elif method == 'turn/start':
        snapshot = next((item['path'] for item in message['params']['input'] if item['type'] == 'localImage'), None)
        emit({'method': 'item/agentMessage/delta', 'params': {'delta': snapshot or 'started'}})
        if scenario == 'starting':
            time.sleep(.3)
        emit({'id': ident, 'result': {'turn': {'id': 'test-turn'}}})
        if scenario == 'approval':
            emit({'id': 101, 'method': 'item/commandExecution/requestApproval', 'params': {'command': 'test'}})
    elif method == 'turn/interrupt':
        if scenario == 'disconnect':
            sys.exit(0)
        if scenario in ('timeout', 'close'):
            time.sleep(3)
        status = 'completed' if scenario == 'completed_race' else 'interrupted'
        completed = {'method': 'turn/completed', 'params': {'turn': {'id': 'test-turn', 'status': status}}}
        response = {'id': ident, 'result': {}}
        if scenario == 'completion_before_ack':
            emit(completed)
            emit(response)
        else:
            emit(response)
            emit(completed)
''' % scenario
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'test-server'
            executable.write_text(program)
            executable.chmod(0o755)
            events, ready, done = [], threading.Event(), threading.Event()
            def emit(kind, value):
                events.append((kind, value))
                if (kind == 'approval' if scenario == 'approval' else kind == 'delta'):
                    ready.set()
                if kind == 'done':
                    done.set()
            b = backend.Backend(emit, auth=FakeAuth(), executable=str(executable))
            images = None
            if scenario == 'close':
                image = Path(directory) / 'input.png'
                image.write_bytes(self.image_bytes())
                images = [str(image)]
            b.send('task', 'test', directory, images=images)
            self.assertTrue(ready.wait(5), events)
            active = b.active
            snapshot = next((value for kind, value in events if kind == 'delta'), None)
            if scenario == 'close':
                self.assertTrue(Path(snapshot).is_file())
            started = time.monotonic()
            if scenario == 'close':
                b.close()
                self.assertLess(time.monotonic() - started, 3)
            else:
                b.cancel()
                self.assertLess(time.monotonic() - started, .2)
            self.assertTrue(done.wait(5), events)
            if scenario == 'close':
                self.assertIsNotNone(active.process.poll())
                self.assertFalse(Path(snapshot).exists())
            return events

    def test_stop_interrupts_active_turn_and_waiting_approval(self):
        for scenario in ('active', 'approval', 'starting'):
            with self.subTest(scenario=scenario):
                events = self._stop_protocol(scenario)
                self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'interrupted'}))
                self.assertFalse(any(event[0] == 'error' for event in events))

    def test_stop_connection_loss_reports_unknown_not_interrupted(self):
        events = self._stop_protocol('disconnect')
        self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'unknown'}))
        self.assertTrue(any(kind == 'error' and '未知' in value for kind, value in events))

    def test_stop_timeout_reports_unknown_not_interrupted(self):
        with mock.patch.object(backend, 'STOP_TIMEOUT_SECONDS', .15):
            events = self._stop_protocol('timeout')
        self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'unknown'}))

    def test_quit_reaps_server_and_cleans_private_snapshots(self):
        events = self._stop_protocol('close')
        self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'unknown'}))

    def test_quit_reaps_delayed_history_server_and_not_unrelated_process(self):
        import os
        import subprocess
        program = """#!/usr/bin/python3
import json, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        result = {}
    elif method == 'account/read':
        result = {'account': {'type': 'chatgpt'}}
    elif method == 'thread/read':
        open(MARKER, 'w').write('reading')
        time.sleep(30)
        result = {'thread': {'id': 'synthetic', 'cwd': '/tmp'}}
    else:
        continue
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
"""
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'synthetic-server'
            marker = Path(directory) / 'reading'
            executable.write_text(program.replace('MARKER', repr(str(marker))))
            executable.chmod(0o700)
            finished = threading.Event()
            events = []
            def emit(event, data):
                events.append(event)
                if event in ('codex_thread', 'codex_thread_error'): finished.set()
            unrelated = subprocess.Popen(['/bin/sleep', '10'])
            b = backend.Backend(emit, auth=FakeAuth(), executable=str(executable))
            try:
                with mock.patch.dict(os.environ, {'CODEX_HOME': directory}):
                    b.codex_thread('synthetic')
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists(), 'synthetic server did not enter read')
                    with b.active_lock:
                        processes = list(b.pending_processes)
                    self.assertEqual(len(processes), 1)
                    started = time.monotonic()
                    b.close()
                    self.assertLess(time.monotonic() - started, 1.5)
                    self.assertTrue(finished.wait(3), events)
                    self.assertIsNotNone(processes[0].poll())
                    self.assertIsNone(unrelated.poll())
                    with b.active_lock:
                        self.assertFalse(b.pending_processes)
            finally:
                b.close()
                unrelated.terminate()
                unrelated.wait(timeout=2)

    def test_quit_reaps_history_server_stuck_in_initialize(self):
        import os
        program = """#!/usr/bin/python3
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    open(MARKER, 'w').write('initializing')
    time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'synthetic-server'
            marker = Path(directory) / 'initializing'
            executable.write_text(program.replace('MARKER', repr(str(marker))))
            executable.chmod(0o700)
            finished = threading.Event()
            b = backend.Backend(lambda event, data: finished.set(), auth=FakeAuth(), executable=str(executable))
            try:
                with mock.patch.dict(os.environ, {'CODEX_HOME': directory}):
                    b.codex_thread('synthetic')
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists())
                    with b.active_lock:
                        processes = list(b.pending_processes)
                    self.assertEqual(len(processes), 1)
                    started = time.monotonic()
                    b.close()
                    self.assertLess(time.monotonic() - started, 1.5)
                    self.assertTrue(finished.wait(3))
                    self.assertIsNotNone(processes[0].poll())
                    with b.active_lock:
                        self.assertFalse(b.pending_processes)
            finally:
                b.close()

    def test_quit_reaps_turn_server_stuck_in_initialize(self):
        import os
        import subprocess
        program = """#!/usr/bin/python3
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    open(MARKER, 'w').write('initializing')
    time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'synthetic-server'
            marker = Path(directory) / 'initializing'
            executable.write_text(program.replace('MARKER', repr(str(marker))))
            executable.chmod(0o700)
            events, done = [], threading.Event()
            def emit(event, data):
                events.append((event, data))
                if event == 'done': done.set()
            unrelated = subprocess.Popen(['/bin/sleep', '10'])
            b = backend.Backend(emit, auth=FakeAuth(), executable=str(executable))
            try:
                with mock.patch.dict(os.environ, {'CODEX_HOME': directory}):
                    b.send('task', 'test', directory)
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists())
                    with b.active_lock:
                        processes = list(b.pending_processes)
                        self.assertEqual(b.active, 'starting')
                    self.assertEqual(len(processes), 1)
                    started = time.monotonic()
                    b.close()
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertTrue(done.wait(3), events)
                    self.assertIsNotNone(processes[0].poll())
                    self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'interrupted'}))
                    self.assertFalse(any(kind == 'error' for kind, _ in events))
                    self.assertIsNone(unrelated.poll())
                    with b.active_lock:
                        self.assertFalse(b.pending_processes)
            finally:
                b.close()
                unrelated.terminate()
                unrelated.wait(timeout=2)

    def test_cancel_during_turn_initialization_remains_interrupted(self):
        import os
        program = """#!/usr/bin/python3
import sys, time
for line in sys.stdin:
    open(MARKER, 'w').write('initializing')
    time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'synthetic-server'
            marker = Path(directory) / 'initializing'
            executable.write_text(program.replace('MARKER', repr(str(marker))))
            executable.chmod(0o700)
            events, done = [], threading.Event()
            def emit(event, data):
                events.append((event, data))
                if event == 'done': done.set()
            b = backend.Backend(emit, auth=FakeAuth(), executable=str(executable))
            try:
                with mock.patch.dict(os.environ, {'CODEX_HOME': directory}):
                    b.send('task', 'test', directory)
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists())
                    with b.active_lock:
                        processes = list(b.pending_processes)
                    self.assertEqual(len(processes), 1)
                    started = time.monotonic()
                    b.cancel()
                    self.assertLess(time.monotonic() - started, .2)
                    self.assertTrue(done.wait(4), events)
                    self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'interrupted'}))
                    self.assertFalse(any(kind == 'error' for kind, _ in events))
                    self.assertIsNotNone(processes[0].poll())
                    with b.active_lock:
                        self.assertFalse(b.pending_processes)
            finally:
                b.close()

    def test_quit_reaps_concurrent_queries_stuck_in_initialize(self):
        import os
        import subprocess
        program = """#!/usr/bin/python3
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    open(os.path.join(MARKER, str(os.getpid())), 'w').write('initializing')
    time.sleep(30)
"""
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'synthetic-server'
            markers = Path(directory) / 'markers'
            markers.mkdir()
            executable.write_text(program.replace('MARKER', repr(str(markers))))
            executable.chmod(0o700)
            finished = threading.Event()
            events = []
            expected = {'account_error', 'limits_error', 'models_error',
                        'codex_threads_error', 'codex_thread_error'}
            def emit(event, data):
                events.append(event)
                if expected.issubset(events):
                    finished.set()
            unrelated = subprocess.Popen(['/bin/sleep', '10'])
            b = backend.Backend(emit, auth=FakeAuth(), executable=str(executable))
            try:
                with mock.patch.dict(os.environ, {'CODEX_HOME': directory}):
                    b.account(); b.limits(); b.models('task'); b.codex_threads(); b.codex_thread('synthetic')
                    deadline = time.monotonic() + 5
                    while len(list(markers.iterdir())) < 5 and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertEqual(len(list(markers.iterdir())), 5)
                    with b.active_lock:
                        processes = list(b.pending_processes)
                    self.assertEqual(len(processes), 5)
                    started = time.monotonic()
                    b.close()
                    self.assertLess(time.monotonic() - started, 1.5)
                    self.assertTrue(finished.wait(3), events)
                    self.assertTrue(all(process.poll() is not None for process in processes))
                    self.assertIsNone(unrelated.poll())
                    with b.active_lock:
                        self.assertFalse(b.pending_processes)
            finally:
                b.close()
                unrelated.terminate()
                unrelated.wait(timeout=2)

    def test_quit_waits_for_spawn_registration_before_taking_snapshot(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'synthetic-server'
            executable.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n')
            executable.chmod(0o700)
            entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
            real_popen = subprocess.Popen
            def delayed_popen(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(3))
                return real_popen(*args, **kwargs)
            b = backend.Backend(lambda *args: None, auth=FakeAuth(), executable=str(executable))
            try:
                with mock.patch.dict(os.environ, {'CODEX_HOME': directory}), \
                        mock.patch.object(backend.subprocess, 'Popen', side_effect=delayed_popen):
                    b.limits()
                    self.assertTrue(entered.wait(3))
                    closer = threading.Thread(target=lambda: (b.close(), stopped.set()))
                    closer.start()
                    self.assertFalse(stopped.wait(.05))
                    release.set()
                    self.assertTrue(stopped.wait(3))
                    closer.join()
                    deadline = time.monotonic() + 3
                    while b.pending_processes and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertFalse(b.pending_processes)
            finally:
                release.set()
                b.close()

    def test_quit_rejects_new_work(self):
        events = []
        b = backend.Backend(lambda *event: events.append(event), auth=FakeAuth())
        b.close()
        b.send('task', 'test', '/tmp')
        self.assertEqual(events[-1][0], 'error')
        self.assertIn('退出', events[-1][1])
        self.assertIsNone(b.active)

    def test_successful_completion_racing_stop_stays_completed(self):
        events = self._stop_protocol('completed_race')
        self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'completed'}))

    def test_completion_before_interrupt_ack_does_not_wait_for_another_event(self):
        events = self._stop_protocol('completion_before_ack')
        self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'interrupted'}))

    def test_stop_before_turn_does_not_launch_request(self):
        events, done = [], threading.Event()
        b = backend.Backend(lambda *event: (events.append(event), done.set() if event[0] == 'done' else None), auth=FakeAuth())
        started, release = threading.Event(), threading.Event()
        def load(_):
            started.set()
            release.wait(2)
            return []
        class NoServer:
            def __init__(self, *args, **kwargs):
                raise AssertionError('server must not start after cancellation')
        with mock.patch.object(backend, 'load_images', side_effect=load), \
             mock.patch.object(backend, 'AppServer', NoServer):
            b.send('task', 'test', '/tmp')
            self.assertTrue(started.wait(2))
            b.cancel()
            release.set()
            self.assertTrue(done.wait(2))
        self.assertEqual(events[-1], ('done', {'mode': 'task', 'status': 'interrupted'}))

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
                    assert params['input'][0]['type'] == 'text' and '默认用简体中文' in params['input'][0]['text']
                    assert params['input'][1] == {'type': 'text', 'text': 'look'}
                    self.snapshot = Path(next(item['path'] for item in params['input'] if item['type'] == 'localImage'))
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
                if method == 'thread/turns/list': return {'data': [], 'nextCursor': None}
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
            self.assertEqual(requests_seen[-1], ('thread/turns/list', {'threadId': 'old', 'limit': 50, 'sortDirection': 'asc', 'itemsView': 'full'}))
            with self.assertRaisesRegex(RuntimeError, '工作目录'):
                b._agent('task', 'do something', directory, thread_id='old', task_sandbox='read-only')
            self.assertNotIn('thread/resume', [method for method, _ in requests_seen])
        b.send('chat', 'hi', None, thread_id='old')
        self.assertIn('只能在任务模式', events[-1][1])

    def test_history_pages_and_legacy_fallback(self):
        events, ready, calls = [], threading.Event(), []
        def emit(event, data):
            events.append((event, data))
            if event in ('codex_thread', 'codex_thread_error'): ready.set()
        class Server:
            fallback = False
            def __init__(self, *args, **kwargs): pass
            def request(self, method, params):
                calls.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/read':
                    return {'thread': {'id': 'chosen', 'cwd': '/tmp', 'turns': [{'id': 'legacy'}] if params.get('includeTurns') else []}}
                if method == 'thread/turns/list':
                    if self.fallback: raise backend.RpcError({'code': -32601, 'message': 'unknown method'})
                    return {'data': [{'id': 'two' if params.get('cursor') else 'one'}],
                            'nextCursor': None if params.get('cursor') else 'next'}
                raise AssertionError(method)
            def close(self): pass
        b = backend.Backend(emit, auth=FakeAuth())
        with mock.patch.object(backend, 'AppServer', Server):
            b.codex_thread('chosen')
            self.assertTrue(ready.wait(2))
            self.assertEqual([t['id'] for t in events[-1][1]['thread']['turns']], ['one', 'two'])
            self.assertEqual([p.get('cursor') for m, p in calls if m == 'thread/turns/list'], [None, 'next'])
            ready.clear(); calls.clear(); Server.fallback = True
            b.codex_thread('chosen')
            self.assertTrue(ready.wait(2))
            self.assertEqual(events[-1][1]['thread']['turns'][0]['id'], 'legacy')
            self.assertEqual(calls[-1], ('thread/read', {'threadId': 'chosen', 'includeTurns': True}))
        self.assertFalse(any(m in ('thread/resume', 'turn/start') for m, p in calls))

    def test_history_bad_cursor_and_non_method_error_are_reported(self):
        events, ready, calls = [], threading.Event(), []
        def emit(event, data):
            events.append((event, data))
            if event in ('codex_thread', 'codex_thread_error'): ready.set()
        class Server:
            fail = False
            def __init__(self, *args, **kwargs): pass
            def request(self, method, params):
                calls.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/read': return {'thread': {'id': 'chosen', 'cwd': '/tmp'}}
                if method == 'thread/turns/list':
                    if self.fail: raise backend.RpcError({'code': -32602, 'message': 'denied'})
                    return {'data': [], 'nextCursor': 'same'}
                raise AssertionError(method)
            def close(self): pass
        b = backend.Backend(emit, auth=FakeAuth())
        with mock.patch.object(backend, 'AppServer', Server):
            b.codex_thread('chosen'); self.assertTrue(ready.wait(2))
            self.assertEqual(events[-1][0], 'codex_thread_error')
            self.assertIn('游标', events[-1][1])
            ready.clear(); calls.clear(); Server.fail = True
            b.codex_thread('chosen'); self.assertTrue(ready.wait(2))
            self.assertEqual(events[-1], ('codex_thread_error', 'denied'))
            self.assertFalse(any(p.get('includeTurns') for m, p in calls))

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
        inputs = requests['turn/start']['input']
        self.assertIn('默认用简体中文', inputs[0]['text'])
        self.assertIn('如果本轮用户要求其他语言或文字，请遵从', inputs[0]['text'])
        self.assertEqual(inputs[1], {'type': 'text', 'text': 'hello'})
        self.assertNotIn('developerInstructions', requests['thread/resume'])
        self.assertEqual(b.threads[('task', directory)], 'selected')

    def test_started_task_emits_its_send_request_id(self):
        done = threading.Event()
        events = []
        class Server:
            def __init__(self, *args, **kwargs):
                self.completed_turns = {}
            def request(self, method, params):
                if method == 'account/read':
                    return {'account': {'type': 'chatgpt'}}
                if method == 'thread/start':
                    return {'thread': {'id': 'new', 'cwd': directory}}
                if method == 'turn/start':
                    self.completed_turns['turn'] = {'id': 'turn', 'status': 'completed'}
                    return {'turn': {'id': 'turn'}}
                raise AssertionError(method)
            def close(self): pass
        def emit(kind, data):
            events.append((kind, data))
            if kind == 'done': done.set()
        b = backend.Backend(emit, auth=FakeAuth())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, 'AppServer', Server):
            request_id = b.send('task', 'hello', directory, model='model')
            self.assertTrue(done.wait(2), events)
            thread = next(data for kind, data in events if kind == 'thread')
            self.assertEqual(thread['request_id'], request_id)
            self.assertEqual(thread['thread'], {'id': 'new', 'cwd': directory})
            self.assertEqual(thread['mode'], 'task')

    def test_own_created_task_can_resume_default_effort_with_explicit_permission(self):
        calls = []
        class Server:
            def __init__(self, *args, **kwargs): self.completed_turns = {}
            def request(self, method, params):
                calls.append((method, params))
                if method == 'account/read': return {'account': {'type': 'chatgpt'}}
                if method == 'thread/start':
                    return {'thread': {'id': 'new', 'cwd': directory}, 'model': 'm', 'reasoningEffort': None}
                if method == 'thread/read':
                    return {'thread': {'id': 'new', 'cwd': directory, 'modelProvider': 'openai',
                                       'model': 'm', 'reasoningEffort': None}}
                if method == 'thread/resume':
                    return {'thread': {'id': 'new', 'cwd': directory}, 'model': 'm', 'reasoningEffort': None,
                            'sandbox': {'type': 'readOnly'}, 'approvalPolicy': 'on-request'}
                if method == 'turn/start':
                    self.completed_turns['turn'] = {'id': 'turn', 'status': 'completed'}
                    return {'turn': {'id': 'turn'}}
                raise AssertionError(method)
            def close(self): pass
        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(backend, 'AppServer', Server):
            self.assertEqual(b._agent('task', 'first', directory, model='m'), 'completed')
            self.assertEqual(b._agent('task', 'follow', directory, model='m', thread_id='new',
                                      task_sandbox='read-only'), 'completed')
            resume = next(params for method, params in calls if method == 'thread/resume')
            self.assertEqual(resume['sandbox'], 'read-only')
            self.assertEqual(resume['approvalPolicy'], 'on-request')
            turns = [params for method, params in calls if method == 'turn/start']
            self.assertNotIn('effort', turns[-1])
            b.new_thread('task', directory)
            with self.assertRaisesRegex(RuntimeError, '历史对话未提供原推理强度'):
                b._agent('task', 'external', directory, model='m', thread_id='new', task_sandbox='read-only')

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
            def __init__(self, emit, executable=None, **kwargs): pass
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
        self.assertIn('默认用简体中文', kwargs['json']['instructions'])
        self.assertIn('如果用户要求其他语言或文字，遵从用户的要求', kwargs['json']['instructions'])
        self.assertEqual(kwargs['json']['input'], [{'role': 'user', 'content': 'hi'}])
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

    def test_chat_language_default_keeps_explicit_user_request_unchanged(self):
        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        models = Response({'models': [{'slug': 'model-one', 'visibility': 'list'}]})
        stream = Response(lines=['data: ' + json.dumps({'type': 'response.completed'})])
        with mock.patch.object(backend.requests, 'get', return_value=models), \
             mock.patch.object(backend.requests, 'post', return_value=stream) as post:
            b._chat('Please answer in English.')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['input'], [{'role': 'user', 'content': 'Please answer in English.'}])
        self.assertIn('默认用简体中文', body['instructions'])
        self.assertIn('遵从用户的要求', body['instructions'])
        self.assertEqual(b.history[0]['content'], 'Please answer in English.')

    def test_chat_http_failures_report_safe_structured_diagnostics(self):
        class FailedResponse(Response):
            def __init__(self, status, obj):
                super().__init__(obj)
                self.status_code = status
                self.headers = {'x-request-id': 'req_0123456789abcdef0123456789abcdef'}
                self.closed = False
            def raise_for_status(self):
                raise backend.requests.HTTPError(response=self)
            def close(self):
                self.closed = True

        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        models = Response({'models': [{'slug': 'one', 'visibility': 'list'}]})
        cases = [
            (401, {'detail': 'Bearer secret-token PRIVATE INPUT'}, '身份或授权'),
            (403, {'error': {'code': 'subscription_sharing_user_not_eligible', 'message': 'PRIVATE INPUT'}}, '工作空间'),
            (429, {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}, '用量已达上限'),
            (503, {'error': {'code': 'subscription_sharing_usage_unavailable'}}, '无法检查'),
            (503, {'error': {'code': 'subscription_sharing_user_unavailable'}}, '无法获取账户或工作空间'),
            (400, {'error': {'code': 'subscription_sharing_unsupported_capability', 'param': 'model'}}, '参数：model'),
        ]
        for status, obj, expected in cases:
            with self.subTest(status=status):
                response = FailedResponse(status, obj)
                with mock.patch.object(backend.requests, 'get', return_value=models), mock.patch.object(backend.requests, 'post', return_value=response):
                    with self.assertRaises(RuntimeError) as caught:
                        b._chat('PRIVATE INPUT')
                text = str(caught.exception)
                self.assertIn(expected, text)
                self.assertIn(f'HTTP {status}', text)
                self.assertIn('req_0123456789abcdef0123456789abcdef', text)
                self.assertNotIn('PRIVATE INPUT', text)
                self.assertNotIn('secret-token', text)
                self.assertEqual(b.history, [])
                self.assertTrue(response.closed)

        response = FailedResponse(403, {'error': {'code': 'secret-token', 'param': 'PRIVATE INPUT'}})
        response.headers['x-request-id'] = 'req_PRIVATE INPUT'
        with mock.patch.object(backend.requests, 'get', return_value=response):
            with self.assertRaises(RuntimeError) as caught:
                b._chat('PRIVATE INPUT')
        self.assertIn('HTTP 403', str(caught.exception))
        self.assertNotIn('PRIVATE INPUT', str(caught.exception))
        self.assertNotIn('secret-token', str(caught.exception))

    def test_chat_stream_failures_distinguish_limits_unsupported_and_incomplete(self):
        b = backend.Backend(lambda *_: None, auth=FakeAuth())
        models = Response({'models': [{'slug': 'one', 'visibility': 'list'}]})
        cases = [
            ({'type': 'response.failed', 'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded', 'message': 'PRIVATE'}}}, '用量已达上限'),
            ({'type': 'error', 'code': 'subscription_sharing_unsupported_capability', 'param': 'tools', 'message': 'PRIVATE'}, '参数：tools'),
            ({'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'PRIVATE'}}}, '回复未完整'),
            ({'type': 'response.failed', 'response': {'error': {'code': 'PRIVATE', 'param': 'PRIVATE'}}}, '请求失败'),
        ]
        for event, expected in cases:
            with self.subTest(event=event['type']):
                stream = Response(lines=['data: ' + json.dumps(event)])
                with mock.patch.object(backend.requests, 'get', return_value=models), mock.patch.object(backend.requests, 'post', return_value=stream):
                    with self.assertRaises(RuntimeError) as caught:
                        b._chat('PRIVATE')
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(b.history, [])

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

    def test_command_approval_uses_matching_started_item_and_rejects_unknown(self):
        events, sent = [], []
        server = object.__new__(backend.AppServer)
        server.emit = lambda *args: events.append(args)
        server.write = sent.append
        server.pending_approvals = {}
        server.mode = 'task'
        ids = {'threadId': 'thread-a', 'turnId': 'turn-a', 'itemId': 'item-a'}
        server.handle({'method': 'item/started', 'params': {
            'threadId': ids['threadId'], 'turnId': ids['turnId'],
            'item': {'id': ids['itemId'], 'type': 'commandExecution',
                     'command': 'rm -i old-file', 'cwd': '/tmp/project', 'aggregatedOutput': 'private'}}})
        server.handle({'id': 1, 'method': 'item/commandExecution/requestApproval', 'params': ids})
        self.assertEqual(events[-1][1]['command_item'], {'command': 'rm -i old-file', 'cwd': '/tmp/project'})
        server.approve(1, 'accept')
        self.assertEqual(sent[-1]['result']['decision'], 'accept')
        for key in ids:
            mismatched = dict(ids, **{key: 'other'})
            server.handle({'id': 2, 'method': 'item/commandExecution/requestApproval', 'params': mismatched})
            self.assertIsNone(events[-1][1]['command_item'])
            with self.assertRaisesRegex(ValueError, '审批详情不完整'):
                server.approve(2, 'accept')
            server.approve(2, 'decline')
        server.handle({'id': 3, 'method': 'item/commandExecution/requestApproval',
                       'params': {'networkApprovalContext': {'host': 'example.com', 'protocol': 'https'}}})
        server.approve(3, 'accept')
        self.assertEqual(sent[-1]['result']['decision'], 'accept')
        server.handle({'id': 4, 'method': 'item/commandExecution/requestApproval',
                       'params': {'command': 'curl', 'networkApprovalContext': {'host': 'example.com'}}})
        with self.assertRaisesRegex(ValueError, '审批详情不完整'):
            server.approve(4, 'accept')
        server.handle({'id': 6, 'method': 'item/commandExecution/requestApproval',
                       'params': dict(ids, approvalId='subcommand-id')})
        with self.assertRaisesRegex(ValueError, '审批详情不完整'):
            server.approve(6, 'accept')
        server.handle({'id': 5, 'method': 'item/commandExecution/requestApproval',
                       'params': dict(ids, kind='writeStdin', command='safe-looking')})
        with self.assertRaisesRegex(ValueError, '审批详情不完整'):
            server.approve(5, 'accept')
        server.handle({'method': 'item/completed', 'params': {'threadId': ids['threadId'],
                      'turnId': ids['turnId'], 'item': {'id': ids['itemId'], 'type': 'commandExecution'}}})
        self.assertFalse(server.command_items)

    def test_command_approval_checks_shown_snapshot_before_accepting(self):
        events, sent = [], []
        server = object.__new__(backend.AppServer)
        server.emit = lambda *args: events.append(args)
        server.write = sent.append
        server.pending_approvals = {}
        server.mode = 'task'
        ids = {'threadId': 't', 'turnId': 'r', 'itemId': 'i'}
        def started(command='echo first', cwd='/tmp'):
            server.handle({'method': 'item/started', 'params': {
                'threadId': 't', 'turnId': 'r', 'item': {'id': 'i', 'type': 'commandExecution',
                                                            'command': command, 'cwd': cwd}}})
        def approval(ident, **extra):
            server.handle({'id': ident, 'method': 'item/commandExecution/requestApproval',
                           'params': dict(ids, **extra)})
        started()
        approval(1)
        started('echo changed')
        server.approve(1, 'accept')
        self.assertEqual(sent[-1], {'id': 1, 'result': {'decision': 'decline'}})
        self.assertNotIn(1, server.pending_approvals)
        self.assertNotIn(1, server.pending_command_snapshots)
        self.assertIn('命令内容发生变化', events[-1][1])
        started()
        approval(2, availableDecisions=['accept', 'cancel'])
        started(None)
        self.assertNotIn(('t', 'r', 'i'), server.command_items)
        server.approve(2, 'accept')
        self.assertEqual(sent[-1]['result']['decision'], 'cancel')
        started()
        approval(3, availableDecisions=['accept'])
        started('x' * (backend.MAX_APPROVAL_PATCH_CHARS + 1))
        server.approve(3, 'accept')
        self.assertIn('error', sent[-1])
        self.assertNotIn(3, server.pending_approvals)
        started()
        approval(4)
        server.handle({'method': 'item/completed', 'params': {'threadId': 't', 'turnId': 'r',
                       'item': {'id': 'i', 'type': 'commandExecution'}}})
        server.approve(4, 'accept')
        self.assertEqual(sent[-1]['result']['decision'], 'decline')
        started()
        approval(5)
        # A repeated identical item remains safe to accept.
        started()
        server.approve(5, 'accept')
        self.assertEqual(sent[-1]['result']['decision'], 'accept')
        started()
        started(cwd=None)
        self.assertNotIn(('t', 'r', 'i'), server.command_items)
        started()
        started(cwd='x' * (backend.MAX_APPROVAL_PATCH_CHARS + 1))
        self.assertNotIn(('t', 'r', 'i'), server.command_items)

    def test_command_approval_accepts_only_server_offered_decisions(self):
        sent = []
        server = object.__new__(backend.AppServer)
        server.write = sent.append
        server.pending_approvals = {1: ('item/commandExecution/requestApproval',
                                        {'availableDecisions': ['decline', 'cancel']})}
        with self.assertRaises(ValueError):
            server.approve(1, 'accept')
        self.assertEqual(sent, [])
        self.assertIn(1, server.pending_approvals)
        server.approve(1, 'decline')
        self.assertEqual(sent, [{'id': 1, 'result': {'decision': 'decline'}}])
        server.pending_approvals[2] = ('item/commandExecution/requestApproval',
                                       {'availableDecisions': [{'applyNetworkPolicyAmendment': {}}]})
        server.approve(2, 'unsupported')
        self.assertEqual(sent[-1]['error']['code'], -32601)
        self.assertNotIn(2, server.pending_approvals)
        server.mode = 'plan'
        server.handle({'id': 3, 'method': 'item/commandExecution/requestApproval',
                       'params': {'availableDecisions': ['cancel']}})
        self.assertEqual(sent[-1]['result']['decision'], 'cancel')
        server.handle({'id': 4, 'method': 'item/commandExecution/requestApproval',
                       'params': {'availableDecisions': ['accept']}})
        self.assertIn('error', sent[-1])

    def test_file_approval_uses_only_matching_current_patch_and_bounded_cache(self):
        events = []
        server = object.__new__(backend.AppServer)
        server.emit = lambda *x: events.append(x)
        server.pending_approvals = {}
        server.completed_turns = {}
        server.mode = 'task'
        ids = {'threadId': 'thread-a', 'turnId': 'turn-a', 'itemId': 'item-a'}
        change = {'path': 'a.py', 'kind': {'type': 'update'}, 'diff': '-before\n+after'}
        server.handle({'method': 'item/started', 'params': {
            'threadId': ids['threadId'], 'turnId': ids['turnId'],
            'item': {'id': ids['itemId'], 'type': 'fileChange', 'changes': [change]}}})
        server.handle({'id': 1, 'method': 'item/fileChange/requestApproval', 'params': ids})
        self.assertEqual(events[-1][1]['file_changes'], [change])
        for key in ids:
            mismatched = dict(ids, **{key: 'other'})
            server.handle({'id': 2, 'method': 'item/fileChange/requestApproval', 'params': mismatched})
            self.assertIsNone(events[-1][1]['file_changes'])
        for key in ids:
            missing = dict(ids)
            del missing[key]
            server.handle({'id': 2, 'method': 'item/fileChange/requestApproval', 'params': missing})
            self.assertIsNone(events[-1][1]['file_changes'])
        updated = dict(change, diff='-before\n+updated')
        server.handle({'method': 'item/fileChange/patchUpdated', 'params': dict(ids, changes=[updated])})
        server.handle({'id': 3, 'method': 'item/fileChange/requestApproval', 'params': ids})
        self.assertEqual(events[-1][1]['file_changes'], [updated])
        sent = []
        server.write = sent.append
        server.approve(1, 'accept')
        self.assertEqual(sent[-1]['result']['decision'], 'decline')
        self.assertIn('审批期间', events[-1][1])
        oversized = dict(change, diff='x' * (backend.MAX_APPROVAL_PATCH_CHARS + 1))
        server.handle({'method': 'item/fileChange/patchUpdated', 'params': dict(ids, changes=[oversized])})
        server.handle({'id': 4, 'method': 'item/fileChange/requestApproval', 'params': ids})
        self.assertIsNone(events[-1][1]['file_changes'])
        server.handle({'method': 'item/fileChange/patchUpdated', 'params': dict(ids, changes=[change])})
        server.handle({'method': 'item/completed', 'params': {'threadId': ids['threadId'], 'turnId': ids['turnId'],
                      'item': {'id': ids['itemId'], 'type': 'fileChange', 'changes': [change]}}})
        server.handle({'id': 5, 'method': 'item/fileChange/requestApproval', 'params': ids})
        self.assertIsNone(events[-1][1]['file_changes'])
        for i in range(backend.MAX_APPROVAL_PATCH_ITEMS + 1):
            server._remember_patch(('thread-a', 'turn-a', str(i)), [change])
        self.assertEqual(len(server.file_change_patches), backend.MAX_APPROVAL_PATCH_ITEMS)
        self.assertNotIn(('thread-a', 'turn-a', '0'), server.file_change_patches)
        server.handle({'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'turn-a'}}})
        self.assertFalse(server.file_change_patches)

    def test_file_approval_rejects_accept_without_full_diff(self):
        events, sent = [], []
        server = object.__new__(backend.AppServer)
        server.emit = lambda *args: events.append(args)
        server.write = sent.append
        server.pending_approvals = {}
        server.mode = 'task'
        ids = {'threadId': 't', 'turnId': 'r', 'itemId': 'i'}
        server.handle({'id': 1, 'method': 'item/fileChange/requestApproval', 'params': ids})
        self.assertIsNone(events[-1][1]['file_changes'])
        with self.assertRaisesRegex(ValueError, '文件差异不可用'):
            server.approve(1, 'accept')
        self.assertEqual(sent, [])
        server.approve(1, 'decline')
        self.assertEqual(sent[-1]['result']['decision'], 'decline')
        server._remember_patch(('t', 'r', 'i'), [{'path': 'a', 'kind': {'type': 'update'}, 'diff': '+x'}])
        server.handle({'id': 2, 'method': 'item/fileChange/requestApproval', 'params': ids})
        server.approve(2, 'accept')
        self.assertEqual(sent[-1]['result']['decision'], 'accept')
        server._remember_patch(('t', 'r', 'i'), [{'path': 'a', 'kind': {'type': 'update'},
                                                 'diff': 'x' * (backend.MAX_APPROVAL_PATCH_CHARS + 1)}])
        server.handle({'id': 3, 'method': 'item/fileChange/requestApproval', 'params': ids})
        with self.assertRaisesRegex(ValueError, '文件差异不可用'):
            server.approve(3, 'accept')
        server.approve(3, 'cancel')
        self.assertEqual(sent[-1]['result']['decision'], 'cancel')

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
            def __init__(self, emit, token, executable, stop_event=None, **kwargs):
                self.token, self.requests, self.mode, self.completed_turns = token, [], None, {}
                Server.last = self
            def request(self, method, params):
                self.requests.append((method, params))
                if method in ('thread/start', 'thread/resume'): return {'thread': {'id': 'thread'}, 'model': 'm'}
                if method == 'turn/start': return {'turn': {'id': 'turn'}}
            def next_event(self, timeout=120): return {'method': 'turn/completed', 'params': {'turn': {'id': 'turn', 'status': self.final_status}}}
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
        self.assertIn('默认用简体中文', turn['input'][0]['text'])
        self.assertEqual(turn['input'][1], {'type': 'text', 'text': 'test'})
        self.assertIsNone(turn['collaborationMode']['settings']['developer_instructions'])
        self.assertNotIn('developerInstructions', server.requests[0][1])
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
