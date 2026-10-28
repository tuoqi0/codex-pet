"""Asynchronous Codex Pet conversation and account bridge.

The callback receives (event, data) on a worker thread; UI must marshal it to
its event loop. Only official ChatGPT OAuth and Codex app-server are used.
"""
import json
import os
import platform
import re
from pathlib import Path
import queue
import shutil
import subprocess
import threading

import requests

from codex_backend_auth import ChatGPTAuth, RESOURCE


def discover_codex(home=None):
    """Find a runnable CLI; prefer PATH then the newest matching VS Code bundle."""
    binary = shutil.which('codex')
    if binary:
        return binary
    architecture = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(platform.machine().lower())
    if not architecture:
        return None
    root = Path(home or Path.home())
    candidates = []
    for extension in (root / '.vscode' / 'extensions').glob('openai.chatgpt-*'):
        try:
            manifest = json.loads((extension / 'package.json').read_text())
        except (OSError, ValueError):
            continue
        if manifest.get('publisher') != 'openai' or manifest.get('name') != 'chatgpt':
            continue
        version = tuple(int(n) for n in re.findall(r'\d+', manifest.get('version', '')))
        candidate = extension / 'bin' / ('linux-' + architecture) / 'codex'
        if version and candidate.is_file() and os.access(candidate, os.X_OK):
            candidates.append((version, str(candidate)))
    return max(candidates)[1] if candidates else None


class AppServer:
    def __init__(self, emit, token=None, executable=None):
        self.emit = emit
        binary = executable or discover_codex()
        if not binary:
            raise RuntimeError('未找到 Codex CLI；请安装官方 Codex CLI 或 VS Code Codex 扩展。')
        args = [binary, 'app-server', '--listen', 'stdio://']
        env = os.environ.copy()
        env.pop('OPENAI_API_KEY', None)
        env.pop('ACCESS_TOKEN', None)
        # Never silently use a custom API provider or a personal API key.
        if token:
            env['ACCESS_TOKEN'] = token
            # Keep plan mode separate from user's Codex configuration, MCP servers and hooks.
            plan_home = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'codex-pet' / 'plan-codex'
            plan_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            if (plan_home / 'config.toml').exists():
                raise RuntimeError('计划模式的隔离配置目录含用户配置；请移走该配置后重试。')
            env['CODEX_HOME'] = str(plan_home)
            args += ['--disable', 'apps', '--disable', 'plugins', '--disable', 'hooks',
                     '--disable', 'computer_use', '--disable', 'browser_use']
            args += ['-c', 'model_provider="openai_chatgpt_plan"',
                     '-c', 'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
                     '-c', 'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
                     '-c', 'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
                     '-c', 'model_providers.openai_chatgpt_plan.wire_api="responses"',
                     '-c', 'model_providers.openai_chatgpt_plan.requires_openai_auth=false',
                     '-c', 'model_providers.openai_chatgpt_plan.supports_websockets=false',
                     '-c', 'shell_environment_policy.exclude=["ACCESS_TOKEN"]',
                     '-c', 'mcp_servers={}']
        else:
            args += ['-c', 'model_provider="openai"']
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
        self.events = queue.Queue()
        self.write_lock = threading.Lock()
        self.counter = 0
        self.pending_approvals = {}
        self.completed_turns = {}
        self.mode = None
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request('initialize', {'clientInfo': {'name': 'codex_pet', 'title': 'Codex Pet', 'version': '2.2.0'}, 'capabilities': {'experimentalApi': True}})
            self.write({'method': 'initialized', 'params': {}})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.events.put(None)

    def write(self, payload):
        with self.write_lock:
            if self.process.poll() is not None:
                raise RuntimeError('Codex App Server 已退出。')
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
            self.process.stdin.flush()

    def request(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.write({'id': request_id, 'method': method, 'params': params})
        while True:
            event = self.next_event()
            if event.get('id') == request_id and 'method' not in event:
                if 'error' in event:
                    raise RuntimeError(event['error'].get('message', 'Codex 请求失败'))
                return event.get('result', {})
            self.handle(event)

    def next_event(self):
        try:
            event = self.events.get(timeout=120)
        except queue.Empty:
            raise RuntimeError('Codex App Server 等待超时。')
        if event is None:
            raise RuntimeError('Codex App Server 已退出。')
        return event

    def handle(self, event):
        method = event.get('method', '')
        params = event.get('params') or {}
        if 'id' in event and method:
            request_id = event['id']
            if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                          'item/permissions/requestApproval', 'item/tool/requestUserInput'):
                if self.mode == 'plan' and method != 'item/tool/requestUserInput':
                    result = {'permissions': {}} if method == 'item/permissions/requestApproval' else {'decision': 'decline'}
                    self.write({'id': request_id, 'result': result})
                else:
                    self.pending_approvals[request_id] = (method, params)
                    self.emit('approval', {'id': request_id, 'method': method, 'params': params})
            else:
                # Do not guess response shapes or grant unknown requests.
                self.write({'id': request_id, 'error': {'code': -32601, 'message': 'Unsupported request'}})
                self.emit('status', '不支持的 Codex 请求已拒绝：' + method)
        elif method == 'turn/completed':
            turn = params.get('turn', {})
            self.completed_turns[turn.get('id')] = turn
        elif method == 'item/agentMessage/delta':
            self.emit('delta', params.get('delta', ''))
        elif method == 'account/rateLimits/updated':
            self.emit('status', 'Codex 额度已更新，请刷新额度面板。')
        elif method in ('item/started', 'item/completed'):
            item = params.get('item', {})
            kind = item.get('type', '')
            if kind in ('commandExecution', 'fileChange', 'webSearch'):
                self.emit('status', 'Codex: ' + kind)
        return method

    def approve(self, request_id, decision):
        if request_id not in self.pending_approvals:
            raise RuntimeError('审批请求已失效。')
        method, params = self.pending_approvals[request_id]
        if method == 'item/tool/requestUserInput':
            if decision == 'cancel':
                payload = {'id': request_id, 'error': {'code': -32800, 'message': 'User cancelled'}}
            elif isinstance(decision, dict) and isinstance(decision.get('answers'), dict):
                expected = {q['id'] for q in params.get('questions', [])}
                if not set(decision['answers']).issubset(expected):
                    raise ValueError('Invalid question id')
                for answer in decision['answers'].values():
                    if not isinstance(answer, dict) or not isinstance(answer.get('answers'), list) or not all(isinstance(a, str) for a in answer['answers']):
                        raise ValueError('Invalid answer')
                payload = {'id': request_id, 'result': decision}
            else:
                raise ValueError('Invalid answer')
        elif method == 'item/permissions/requestApproval':
            if decision not in ('accept', 'decline', 'cancel'):
                raise ValueError('Invalid approval decision')
            permissions = params.get('permissions', {}) if decision == 'accept' else {}
            payload = {'id': request_id, 'result': {'permissions': permissions, 'scope': 'turn'}}
        else:
            if decision not in ('accept', 'decline', 'cancel'):
                raise ValueError('Invalid approval decision')
            payload = {'id': request_id, 'result': {'decision': decision}}
        self.write(payload)
        del self.pending_approvals[request_id]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()


class Backend:
    def __init__(self, emit, auth=None, executable=None):
        self.emit = emit
        self.auth = auth or ChatGPTAuth()
        self.executable = executable
        self.active = None
        self.active_lock = threading.Lock()
        self.cancelled = threading.Event()
        self.response = None
        self.history = []
        self.threads = {}

    def _async(self, operation):
        def run():
            try:
                operation()
            except Exception as exc:
                self.emit('error', str(exc))
        threading.Thread(target=run, daemon=True).start()

    def login(self):
        def run():
            self.emit('status', '请在浏览器完成 ChatGPT 授权。')
            self.auth.login()
            self.emit('account', self.auth.account())
        self._async(run)

    def account(self):
        def run():
            self.emit('account', self.auth.account())
            server = AppServer(self.emit, executable=self.executable)
            try:
                account = server.request('account/read', {'refreshToken': False}).get('account')
                self.emit('account', {'source': 'Codex', 'account': account})
            finally:
                server.close()
        self._async(run)

    def limits(self):
        def run():
            server = AppServer(self.emit, executable=self.executable)
            try:
                account = server.request('account/read', {'refreshToken': False}).get('account')
                if not account or account.get('type') != 'chatgpt':
                    raise RuntimeError('Codex 未以 ChatGPT 账户登录；不会使用 API key。')
                self.emit('limits', server.request('account/rateLimits/read', {}))
            finally:
                server.close()
        self._async(run)

    def _check_identity(self, account=None):
        chat = self.auth.account()
        if not chat.get('signedIn'):
            self.emit('status', 'ChatGPT 独立授权未登录，无法核对它与 Codex 是否为同一账户。')
            return
        if account is None:
            try:
                server = AppServer(self.emit, executable=self.executable)
                try:
                    account = server.request('account/read', {'refreshToken': False}).get('account')
                finally:
                    server.close()
            except Exception:
                self.emit('status', '无法读取 Codex 账户，两个账户是否一致尚未验证。')
                return
        chat_email = chat.get('email')
        codex_email = account.get('email') if account and account.get('type') == 'chatgpt' else None
        if not chat_email or not codex_email:
            self.emit('status', '账户邮箱不可用，无法确认 ChatGPT 与 Codex 是否一致。')
            return
        if chat_email.strip().casefold() != codex_email.strip().casefold():
            raise RuntimeError('ChatGPT 授权账户与 Codex 登录账户不一致；请在 Codex 或桌宠中重新登录同一账户。')
        self.emit('status', 'ChatGPT 与 Codex 的账户邮箱一致；工作空间是否一致未验证。')

    def send(self, mode, text, cwd):
        if mode not in ('chat', 'plan', 'task') or not text.strip():
            self.emit('error', '请选择有效模式并输入内容。')
            return
        with self.active_lock:
            if self.active is not None:
                self.emit('error', '已有请求正在运行。')
                return
            self.active = 'starting'
            self.cancelled.clear()
        def run():
            status = 'failed'
            try:
                if mode in ('chat', 'plan'):
                    self._check_identity()
                if self.cancelled.is_set():
                    status = 'interrupted'
                    return
                if mode == 'chat':
                    self._chat(text)
                    status = 'completed'
                else:
                    status = self._agent(mode, text, cwd)
                if self.cancelled.is_set():
                    status = 'interrupted'
            except Exception as exc:
                if not self.cancelled.is_set():
                    self.emit('error', str(exc))
                else:
                    status = 'interrupted'
            finally:
                with self.active_lock:
                    self.active = None
                    self.response = None
                self.emit('done', {'mode': mode, 'status': status})
        threading.Thread(target=run, daemon=True).start()

    def _chat(self, text):
        token = self.auth.token()
        headers = {'Authorization': 'Bearer ' + token}
        models_response = requests.get(RESOURCE + '/models', headers=headers, timeout=30)
        models_response.raise_for_status()
        models = [m for m in models_response.json().get('models', []) if m.get('visibility') == 'list']
        if not models:
            raise RuntimeError('ChatGPT 账户没有返回可用模型。')
        if self.cancelled.is_set():
            return
        inputs = self.history + [{'role': 'user', 'content': text}]
        response = requests.post(RESOURCE + '/responses', headers=headers,
            json={'model': models[0]['slug'], 'input': inputs, 'store': False, 'stream': True},
            stream=True, timeout=(30, 30))
        self.response = response
        response.raise_for_status()
        answer, completed = [], False
        try:
            for line in response.iter_lines(decode_unicode=True):
                if self.cancelled.is_set():
                    return
                if not line or not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    continue
                event = json.loads(data)
                kind = event.get('type')
                if kind == 'response.output_text.delta':
                    delta = event.get('delta', '')
                    answer.append(delta)
                    self.emit('delta', delta)
                elif kind == 'response.completed':
                    completed = True
                elif kind in ('response.failed', 'response.incomplete', 'error'):
                    raise RuntimeError('ChatGPT 请求失败或额度不可用，请检查 ChatGPT 用量。')
        finally:
            response.close()
        if not completed:
            raise RuntimeError('ChatGPT 流未完整结束，请重试。')
        self.history = inputs + [{'role': 'assistant', 'content': ''.join(answer)}]

    def _agent(self, mode, text, cwd):
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise RuntimeError('请选择有效的工作目录。')
        token = self.auth.token() if mode == 'plan' else None
        server = AppServer(self.emit, token=token, executable=self.executable)
        server.mode = mode
        with self.active_lock:
            self.active = server
        try:
            if self.cancelled.is_set():
                return
            if mode == 'task':
                account = server.request('account/read', {'refreshToken': False}).get('account')
                if not account or account.get('type') != 'chatgpt':
                    raise RuntimeError('Codex 未以 ChatGPT 账户登录；不会使用 API key。')
                self._check_identity(account)
                self.emit('account', {'source': 'Codex', 'account': account})
            else:
                self.emit('account', self.auth.account())
            params = {'cwd': str(directory), 'approvalPolicy': 'on-request',
                      'approvalsReviewer': 'user', 'sandbox': 'read-only' if mode == 'plan' else 'workspace-write'}
            if mode == 'plan':
                # App-server's bundled catalog is not an entitlement check.
                models = requests.get(RESOURCE + '/models', headers={'Authorization': 'Bearer ' + token}, timeout=30)
                models.raise_for_status()
                available = [m for m in models.json().get('models', []) if m.get('visibility') == 'list']
                if not available:
                    raise RuntimeError('ChatGPT 账户没有返回可用模型。')
                params['model'] = available[0]['slug']
            key = (mode, str(directory))
            if key in self.threads:
                started = server.request('thread/resume', {'threadId': self.threads[key], **params})
            else:
                started = server.request('thread/start', params)
                self.threads[key] = started['thread']['id']
            thread = started['thread']
            start = {'threadId': thread['id'], 'input': [{'type': 'text', 'text': text}]}
            if mode == 'plan':
                start['collaborationMode'] = {'mode': 'plan', 'settings': {'model': params['model'], 'developer_instructions': None}}
                start['sandboxPolicy'] = {'type': 'readOnly', 'networkAccess': False}
                start['approvalPolicy'] = 'never'
            turn = server.request('turn/start', start)['turn']
            if self.cancelled.is_set():
                server.request('turn/interrupt', {'threadId': thread['id'], 'turnId': turn['id']})
            while True:
                result = server.completed_turns.pop(turn['id'], None)
                if result is not None:
                    turn_status = result.get('status')
                    if turn_status in ('completed', 'interrupted'):
                        return turn_status
                    if self.cancelled.is_set():
                        return 'interrupted'
                    raise RuntimeError('Codex 任务未完成：' + str(turn_status))
                server.handle(server.next_event())
        finally:
            server.close()

    def cancel(self):
        self.cancelled.set()
        with self.active_lock:
            active = self.active
            response = self.response
        if isinstance(active, AppServer):
            active.close()
        if response is not None:
            response.close()

    def approve(self, request_id, decision):
        with self.active_lock:
            active = self.active
        if isinstance(active, AppServer):
            try:
                active.approve(request_id, decision)
            except Exception as exc:
                self.emit('error', str(exc))

    def close(self):
        self.cancel()
