"""Official Sign in with ChatGPT public-client flow for Codex Pet.

Credentials belong to this application, not Codex's private credential store.
"""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, urlparse
import uuid
import webbrowser

import jwt
import requests

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
AUTH = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
JWKS = ISSUER + '/.well-known/jwks.json'


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


class ChatGPTAuth:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path.home() / '.config' / 'codex-pet')
        self.lock = threading.RLock()
        self.path = self.directory / 'chatgpt-session.json'
        self.host_path = self.directory / 'host-id'

    @contextmanager
    def _process_lock(self):
        # Refresh tokens rotate; serialize updates even if two pets are launched.
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory / '.session.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write(self, path, text):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.tmp-' + secrets.token_hex(8))
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def _host(self):
        if self.host_path.exists():
            return self.host_path.read_text().strip()
        host = 'urn:uuid:' + str(uuid.uuid4())
        self._write(self.host_path, host)
        return host

    def _record(self):
        if not self.path.exists():
            raise RuntimeError('请先使用 Continue with ChatGPT 登录。')
        return json.loads(self.path.read_text())

    def account(self):
        try:
            record = self._record()
        except RuntimeError:
            return {'source': 'ChatGPT plan', 'signedIn': False}
        return {'source': 'ChatGPT plan', 'signedIn': True, 'email': record.get('email')}

    def login(self):
        with self.lock, self._process_lock():
            self._login()

    def _login(self):
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        received = {}
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                if url.path != '/auth/callback':
                    self.send_error(404)
                    return
                values = parse_qs(url.query)
                if done.is_set():
                    self.send_error(409, 'Authorization already completed')
                    return
                if values.get('state', [''])[0] != state:
                    self.send_error(400, 'Invalid state')
                    return
                received.update({k: v[0] for k, v in values.items()})
                body = 'Sign-in received. You may close this window.'.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done.set()

            def log_message(self, *args):
                pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        with Server(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            redirect = f'http://127.0.0.1:{server.server_address[1]}/auth/callback'
            # Reauthorize the existing registration rather than replacing its identity.
            try:
                old = self._record()
            except RuntimeError:
                old = None
            client = old['client_id'] if old else 'dynamic_agent_client'
            params = {'client_id': client, 'ext_agent_host_id': self._host(),
                      'response_type': 'code', 'redirect_uri': redirect, 'scope': SCOPES,
                      'resource': RESOURCE, 'state': state, 'nonce': nonce,
                      'code_challenge_method': 'S256',
                      'code_challenge': _b64(hashlib.sha256(verifier.encode()).digest())}
            if old:
                if old.get('id_token'):
                    params['id_token_hint'] = old['id_token']
            else:
                params['agent_name_hint'] = 'codex_pet'
            try:
                if not webbrowser.open(AUTH + '?' + urlencode(params)):
                    raise RuntimeError('无法打开浏览器完成 ChatGPT 授权。')
                if not done.wait(300):
                    raise RuntimeError('ChatGPT 授权超时，请重试。')
            finally:
                server.shutdown()
                thread.join()
        if received.get('error'):
            raise RuntimeError('ChatGPT 授权未完成：' + received['error'])
        issued = received.get('client_id', client if old else '')
        if not issued or issued == 'dynamic_agent_client' or (old and issued != client):
            raise RuntimeError('ChatGPT 返回的客户端注册信息不匹配。')
        response = requests.post(TOKEN, data={'grant_type': 'authorization_code',
            'client_id': issued, 'code': received.get('code', ''), 'code_verifier': verifier,
            'redirect_uri': redirect, 'resource': RESOURCE}, timeout=30)
        response.raise_for_status()
        tokens = response.json()
        if 'chatgpt.tokens.use.direct' not in tokens.get('scope', '').split():
            raise RuntimeError('没有获得 ChatGPT 计划使用授权。')
        signing_key = jwt.PyJWKClient(JWKS, timeout=15).get_signing_key_from_jwt(tokens['id_token'])
        claims = jwt.decode(tokens['id_token'], signing_key.key, algorithms=['RS256'],
                            audience=issued, issuer=ISSUER,
                            options={'require': ['exp', 'sub', 'iss', 'aud', 'nonce']})
        if claims['nonce'] != nonce or (old and claims['sub'] != old['subject']):
            raise RuntimeError('ChatGPT 身份验证失败或账户不匹配。')
        record = dict(tokens, client_id=issued, subject=claims['sub'], email=claims.get('email'),
                      saved_at=time.time())
        self._write(self.path, json.dumps(record))

    def token(self):
        with self.lock, self._process_lock():
            record = self._record()
            if 'chatgpt.tokens.use.direct' not in record.get('scope', '').split():
                raise RuntimeError('没有获得 ChatGPT 计划使用授权，请重新登录。')
            if time.time() >= record['saved_at'] + record['expires_in'] - 60:
                response = requests.post(TOKEN, data={'grant_type': 'refresh_token',
                    'client_id': record['client_id'], 'refresh_token': record['refresh_token'],
                    'resource': RESOURCE}, timeout=30)
                response.raise_for_status()
                updated = response.json()
                if 'chatgpt.tokens.use.direct' not in updated.get('scope', record['scope']).split():
                    raise RuntimeError('ChatGPT 计划授权已失效。')
                record.update(updated, saved_at=time.time())
                self._write(self.path, json.dumps(record))
            return record['access_token']
