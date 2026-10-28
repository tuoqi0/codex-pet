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
        self.assertEqual(events[-1][0], 'error')
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
