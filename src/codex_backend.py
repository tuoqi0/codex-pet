"""Asynchronous Codex Pet conversation and account bridge.

The callback receives (event, data) on a worker thread; UI must marshal it to
its event loop. Only official ChatGPT OAuth and Codex app-server are used.
"""
import json
import base64
import io
import os
import platform
import re
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

import requests
from PIL import Image, UnidentifiedImageError

from codex_backend_auth import ChatGPTAuth, RESOURCE

MAX_IMAGE_COUNT = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 20 * 1024 * 1024
MAX_CHAT_IMAGE_BYTES = 40 * 1024 * 1024
IMAGE_FORMATS = {'PNG': ('image/png', '.png'), 'JPEG': ('image/jpeg', '.jpg'),
                 'WEBP': ('image/webp', '.webp'), 'GIF': ('image/gif', '.gif')}

STOP_TIMEOUT_SECONDS = 8
MAX_HISTORY_PAGES = 100

CHATGPT_ERROR_HINTS = {
    'subscription_sharing_user_not_eligible': '当前账户、工作空间或策略不允许使用 ChatGPT 计划。',
    'subscription_sharing_usage_limit_exceeded': 'ChatGPT 计划或应用用量已达上限，请在 ChatGPT 用量设置中查看。',
    'subscription_sharing_usage_unavailable': '暂时无法检查 ChatGPT 用量，请稍后重试。',
    'subscription_sharing_user_unavailable': '暂时无法获取账户或工作空间信息，请稍后重试。',
    'subscription_sharing_unsupported_capability': '所选模型或请求能力不受支持，请检查模型及附件。',
    'subscription_sharing_route_not_supported': 'ChatGPT 计划不支持此请求路径，请检查应用配置。',
    'subscription_sharing_invalid_user': 'ChatGPT 账户身份无法验证，请检查授权。',
    'chatpass_v2_scope_not_authorized': 'ChatGPT 授权范围不足，请检查授权。',
    'chatpass_v2_invalid_authorization_context': 'ChatGPT 授权上下文无效，请检查授权。',
}
CHATGPT_HTTP_HINTS = {
    400: '请求或所选能力不受支持，请检查模型及附件。',
    401: 'ChatGPT 身份或授权未被接受，请检查账户授权。',
    403: 'ChatGPT 拒绝了请求，请检查账户、工作空间或地区限制。',
    429: '请求受到限制，请稍后重试并检查 ChatGPT 用量。',
    503: 'ChatGPT 服务或用量检查暂不可用，请稍后重试。',
}


def chatgpt_error_message(status=None, body=None, request_id=None, incomplete=False):
    """Explain documented failures without reflecting arbitrary server text or user data."""
    body = body if isinstance(body, dict) else {}
    error = body.get('error') if isinstance(body.get('error'), dict) else {}
    code = error.get('code')
    known_code = code if isinstance(code, str) and code in CHATGPT_ERROR_HINTS else None
    hint = (CHATGPT_ERROR_HINTS.get(known_code) or
            ('ChatGPT 回复未完整结束，请重试。' if incomplete else
             CHATGPT_HTTP_HINTS.get(status, 'ChatGPT 请求失败，请稍后重试。')))
    details = []
    if isinstance(status, int) and 100 <= status <= 599:
        details.append(f'HTTP {status}')
    if known_code:
        details.append(known_code)
    # Known parameter names convey useful diagnostics without echoing text or paths.
    param = error.get('param')
    if isinstance(param, str) and param in {'model', 'input', 'tools', 'service_tier'}:
        details.append('参数：' + param)
    if isinstance(request_id, str) and re.fullmatch(r'req_[a-fA-F0-9]{16,64}', request_id):
        details.append('请求 ID：' + request_id)
    return hint + ('（' + '；'.join(details) + '）' if details else '')


def chatgpt_http_error(response):
    try:
        body = response.json()
    except (ValueError, requests.RequestException):
        body = None
    headers = getattr(response, 'headers', {}) or {}
    message = chatgpt_error_message(getattr(response, 'status_code', None), body,
                                    headers.get('x-request-id'))
    if isinstance(body, dict):
        # Report only the response shape: the values may contain user data.
        if isinstance(body.get('error'), dict):
            message += '（结构化错误）'
        elif 'detail' in body:
            message += '（服务详情响应）'
    return message


class RpcError(RuntimeError):
    """An app-server JSON-RPC error, retaining the server error code."""

    def __init__(self, error):
        super().__init__(error.get('message', 'Codex 请求失败'))
        self.code = error.get('code')


class CancelledBeforeTurn(Exception):
    """The user stopped before a turn was sent."""


class TurnStateUnknown(RuntimeError):
    """The server did not confirm the outcome of a submitted turn."""


def load_images(paths):
    """Read only explicitly selected images, bounded and validated before a request."""
    if paths is None:
        return []
    if not isinstance(paths, (list, tuple)) or len(paths) > MAX_IMAGE_COUNT:
        raise ValueError('每次最多选择 4 张本地图片。')
    images, total = [], 0
    for path in paths:
        if not isinstance(path, (str, os.PathLike)) or not str(path):
            raise ValueError('请选择有效的本地图片文件。')
        try:
            with open(path, 'rb') as image_file:
                data = image_file.read(MAX_IMAGE_BYTES + 1)
        except OSError as exc:
            raise ValueError('无法读取所选图片，请检查文件是否仍存在且可读取。') from exc
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError('图片为空或超过单张 10 MiB 的限制。')
        total += len(data)
        if total > MAX_IMAGE_TOTAL_BYTES:
            raise ValueError('本次图片总大小超过 20 MiB。')
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in IMAGE_FORMATS:
                    raise ValueError('仅支持 PNG、JPEG、WebP 和静态 GIF 图片。')
                if getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('暂不支持动态图，请选择静态图片。')
                mime, suffix = IMAGE_FORMATS[image.format]
                image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ValueError('图片无效或尺寸过大，请选择有效的静态图片。') from exc
        images.append((mime, suffix, data))
    return images


def ensure_image_supported(entry, field):
    """Missing metadata is unknown; an explicit text-only catalog entry is not."""
    modalities = entry.get(field)
    if isinstance(modalities, list) and 'image' not in modalities:
        raise RuntimeError('所选模型不支持图片输入，请选择支持图片的模型。')


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
    def __init__(self, emit, token=None, executable=None, stop_event=None, on_spawn=None):
        self.emit = emit
        self.stop_event = stop_event
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
            if on_spawn is not None:
                on_spawn(self.process)
            self.request('initialize', {'clientInfo': {'name': 'codex_pet', 'title': 'Codex Pet', 'version': '2.11.0'}, 'capabilities': {'experimentalApi': True}})
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
        if self.stop_event and self.stop_event.is_set() and method not in ('turn/start', 'turn/interrupt'):
            raise CancelledBeforeTurn()
        self.counter += 1
        request_id = self.counter
        self.write({'id': request_id, 'method': method, 'params': params})
        cancel_deadline = None
        deadline = time.monotonic() + 120
        while True:
            if self.stop_event and self.stop_event.is_set():
                if method not in ('turn/start', 'turn/interrupt'):
                    raise CancelledBeforeTurn()
                if cancel_deadline is None:
                    cancel_deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
            remaining = (cancel_deadline or deadline) - time.monotonic()
            if remaining <= 0:
                if cancel_deadline is not None:
                    raise TurnStateUnknown('停止请求未得到确认；任务执行状态未知。')
                raise RuntimeError('Codex App Server 等待超时。')
            try:
                event = self.events.get(timeout=min(remaining, .2))
            except queue.Empty:
                continue
            if event is None:
                if cancel_deadline is not None:
                    raise TurnStateUnknown('停止请求未得到确认；任务执行状态未知。')
                raise RuntimeError('Codex App Server 已退出。')
            if event.get('id') == request_id and 'method' not in event:
                if 'error' in event:
                    raise RpcError(event['error'])
                return event.get('result', {})
            self.handle(event)

    def next_event(self, timeout=120):
        try:
            event = self.events.get(timeout=timeout)
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
                    allowed = params.get('availableDecisions') if method == 'item/commandExecution/requestApproval' else None
                    if allowed is not None and (not isinstance(allowed, list) or not any(
                            decision in allowed for decision in ('decline', 'cancel'))):
                        self.write({'id': request_id, 'error': {'code': -32601, 'message': 'No supported denial decision'}})
                    else:
                        decision = 'cancel' if allowed is not None and 'decline' not in allowed else 'decline'
                        result = {'permissions': {}} if method == 'item/permissions/requestApproval' else {'decision': decision}
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
        elif method == 'item/plan/delta':
            self.emit('progress', {'kind': 'plan_delta', 'item_id': params.get('itemId'),
                                   'delta': params.get('delta', '')})
        elif method == 'turn/plan/updated':
            self.emit('progress', {'kind': 'plan', 'steps': params.get('plan', []),
                                   'explanation': params.get('explanation')})
        elif method == 'model/rerouted':
            self.emit('progress', {'kind': 'model_rerouted', 'from_model': params.get('fromModel'),
                                   'to_model': params.get('toModel'), 'reason': params.get('reason')})
        elif method == 'account/rateLimits/updated':
            self.emit('status', 'Codex 额度已更新，请刷新额度面板。')
        elif method in ('item/started', 'item/completed'):
            item = params.get('item', {})
            kind = item.get('type', '')
            if kind == 'plan' and method == 'item/completed':
                self.emit('progress', {'kind': 'plan_final', 'item_id': item.get('id'),
                                       'text': item.get('text', '')})
            elif kind in ('commandExecution', 'fileChange', 'webSearch', 'mcpToolCall',
                           'dynamicToolCall', 'contextCompaction'):
                # Do not relay command output, patch contents, or tool arguments into
                # the always-visible progress display; approvals retain full details.
                detail = ''
                if kind == 'fileChange':
                    changes = item.get('changes') or []
                    paths = [str(change['path']) for change in changes
                             if isinstance(change, dict) and change.get('path')]
                    detail = '\n'.join(paths[:20])
                    if len(paths) > 20:
                        detail += '\n…'
                elif kind == 'mcpToolCall':
                    detail = ' / '.join(str(item[key]) for key in ('server', 'tool') if item.get(key))
                self.emit('progress', {'kind': 'item',
                                       'phase': 'started' if method == 'item/started' else 'completed',
                                       'item_id': item.get('id'), 'type': kind,
                                       'status': item.get('status'), 'detail': detail})
        return method

    def wake(self):
        """Wake the sole event consumer after a stop request."""
        self.events.put({'_pet_wake': True})

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
            if decision == 'unsupported' and method == 'item/commandExecution/requestApproval':
                payload = {'id': request_id, 'error': {'code': -32601, 'message': 'No supported approval decision selected'}}
            else:
                if decision not in ('accept', 'decline', 'cancel'):
                    raise ValueError('Invalid approval decision')
                allowed = params.get('availableDecisions') if method == 'item/commandExecution/requestApproval' else None
                if allowed is not None and (not isinstance(allowed, list) or decision not in allowed):
                    raise ValueError('This decision was not offered by Codex')
                payload = {'id': request_id, 'result': {'decision': decision}}
        self.write(payload)
        del self.pending_approvals[request_id]

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
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
        self.worker = None
        self.closing = False
        self.history_processes = set()
        self.history = []
        self.history_image_bytes = 0
        self.threads = {}

    def _async(self, operation, error_event='error'):
        def run():
            try:
                operation()
            except Exception as exc:
                self.emit(error_event, str(exc))
        threading.Thread(target=run, daemon=True).start()

    def login(self):
        def run():
            self.emit('status', '请在浏览器完成 ChatGPT 授权。')
            self.auth.login()
            self.emit('account', self.auth.account())
            self.emit('login_complete', None)
        self._async(run, 'login_error')

    def account(self):
        def run():
            self.emit('account', self.auth.account())
            server = AppServer(self.emit, executable=self.executable)
            try:
                account = server.request('account/read', {'refreshToken': False}).get('account')
                self.emit('account', {'source': 'Codex', 'account': account})
            finally:
                server.close()
        self._async(run, 'account_error')

    def limits(self):
        def run():
            server = AppServer(self.emit, executable=self.executable)
            try:
                account = server.request('account/read', {'refreshToken': False}).get('account')
                if not account or account.get('type') != 'chatgpt':
                    raise RuntimeError('Codex 未以 ChatGPT 账户登录；不会使用 API key。')
                result = server.request('account/rateLimits/read', {})
            finally:
                server.close()
            self.emit('limits', result)
        self._async(run, 'limits_error')

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

    def models(self, mode, generation=None):
        """List the selected mode's models without switching its credentials."""
        if mode not in ('chat', 'plan', 'task'):
            self.emit('models_error', {'mode': mode, 'generation': generation, 'message': '请选择有效模式。'})
            return

        def run():
            try:
                if mode in ('chat', 'plan'):
                    entries = self._chatgpt_models(self.auth.token())
                    models = [{'id': entry['slug'],
                               'name': entry.get('display_name') or entry['slug']}
                              for entry in entries]
                else:
                    server = AppServer(self.emit, executable=self.executable)
                    try:
                        account = server.request('account/read', {'refreshToken': False}).get('account')
                        if not account or account.get('type') != 'chatgpt':
                            raise RuntimeError('Codex 未以 ChatGPT 账户登录；不会使用 API key。')
                        models, cursor = [], None
                        while True:
                            params = {'limit': 100, 'includeHidden': False}
                            if cursor:
                                params['cursor'] = cursor
                            result = server.request('model/list', params)
                            for entry in result.get('data', []):
                                if not entry.get('hidden') and (entry.get('model') or entry.get('id')):
                                    model_id = entry.get('model') or entry['id']
                                    models.append({'id': model_id,
                                                   'name': entry.get('displayName') or model_id,
                                                   'efforts': [item['reasoningEffort'] for item in entry.get('supportedReasoningEfforts', []) if item.get('reasoningEffort')],
                                                   'default_effort': entry.get('defaultReasoningEffort'),
                                                   'default': bool(entry.get('isDefault'))})
                            cursor = result.get('nextCursor')
                            if not cursor:
                                break
                    finally:
                        server.close()
                if not models:
                    raise RuntimeError('账户没有返回可选择的模型。')
                self.emit('models', {'mode': mode, 'generation': generation, 'models': models})
            except Exception as exc:
                self.emit('models_error', {'mode': mode, 'generation': generation, 'message': str(exc)})
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _chatgpt_models(token):
        response = requests.get(RESOURCE + '/models', headers={'Authorization': 'Bearer ' + token}, timeout=30)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(chatgpt_http_error(response)) from exc
        models = [entry for entry in response.json().get('models', [])
                  if entry.get('visibility') == 'list' and entry.get('slug')]
        if not models:
            raise RuntimeError('ChatGPT 账户没有返回可用模型。')
        return models

    def codex_threads(self, cursor=None):
        """List local Codex sessions only after the user opens the session picker."""
        def run():
            server = AppServer(self.emit, executable=self.executable)
            try:
                account = server.request('account/read', {'refreshToken': False}).get('account')
                if not account or account.get('type') != 'chatgpt':
                    raise RuntimeError('Codex 未以 ChatGPT 账户登录；不会使用 API key。')
                params = {'limit': 50, 'sourceKinds': ['cli', 'vscode', 'appServer']}
                if cursor:
                    params['cursor'] = cursor
                result = server.request('thread/list', params)
            finally:
                server.close()
            self.emit('codex_threads', result)
        self._async(run, 'codex_threads_error')

    def codex_thread(self, thread_id):
        """Read history only after the user explicitly chooses that session."""
        def run():
            if not thread_id:
                raise RuntimeError('请选择 Codex 对话。')
            registered = []
            def register(process):
                with self.active_lock:
                    if self.closing:
                        raise RuntimeError('应用正在退出。')
                    self.history_processes.add(process)
                    registered.append(process)
            server = None
            try:
                with self.active_lock:
                    if self.closing:
                        raise RuntimeError('应用正在退出。')
                server = AppServer(self.emit, executable=self.executable, on_spawn=register)
                account = server.request('account/read', {'refreshToken': False}).get('account')
                if not account or account.get('type') != 'chatgpt':
                    raise RuntimeError('Codex 未以 ChatGPT 账户登录；不会使用 API key。')
                result = server.request('thread/read', {'threadId': thread_id})
                thread = result.get('thread')
                if not isinstance(thread, dict) or thread.get('id') != thread_id:
                    raise RuntimeError('无法确认所选 Codex 对话。')
                turns, cursor, seen = [], None, set()
                while True:
                    params = {'threadId': thread_id, 'limit': 50,
                              'sortDirection': 'asc', 'itemsView': 'full'}
                    if cursor:
                        params['cursor'] = cursor
                    try:
                        page = server.request('thread/turns/list', params)
                    except RpcError as exc:
                        if exc.code != -32601 or cursor:
                            raise
                        # Older app-server builds predate thread/turns/list.
                        result = server.request('thread/read', {'threadId': thread_id, 'includeTurns': True})
                        break
                    data = page.get('data')
                    if not isinstance(data, list):
                        raise RuntimeError('Codex 历史格式无效。')
                    turns.extend(data)
                    cursor = page.get('nextCursor')
                    if not cursor:
                        thread['turns'] = turns
                        break
                    if not isinstance(cursor, str) or cursor in seen:
                        raise RuntimeError('Codex 历史分页游标无效。')
                    seen.add(cursor)
                    if len(seen) >= MAX_HISTORY_PAGES:
                        raise RuntimeError('此 Codex 对话过长，超过桌宠一次读取上限；原有显示和草稿未更改。')
            finally:
                try:
                    if server is not None:
                        server.close()
                finally:
                    with self.active_lock:
                        for process in registered:
                            self.history_processes.discard(process)
            self.emit('codex_thread', result)
        self._async(run, 'codex_thread_error')

    def new_thread(self, mode, cwd):
        """Forget the active local mapping; existing Codex history is retained."""
        if mode in ('plan', 'task') and cwd:
            self.threads.pop((mode, str(Path(cwd).expanduser().resolve())), None)
        elif mode == 'chat':
            self.history = []
            self.history_image_bytes = 0

    def send(self, mode, text, cwd, model=None, effort=None, thread_id=None, task_sandbox=None, images=None):
        if mode not in ('chat', 'plan', 'task') or (not text.strip() and not images):
            self.emit('error', '请选择有效模式并输入内容或添加图片。')
            return
        if thread_id and mode != 'task':
            self.emit('error', 'Codex 对话只能在任务模式中继续；计划和普通对话使用独立授权。')
            return
        with self.active_lock:
            if self.closing:
                self.emit('error', '桌宠正在退出。')
                return
            if self.active is not None:
                self.emit('error', '已有请求正在运行。')
                return
            self.active = 'starting'
            self.cancelled.clear()
        def run():
            status = 'failed'
            try:
                loaded_images = load_images(images)
                if mode in ('chat', 'plan'):
                    self._check_identity()
                if self.cancelled.is_set():
                    status = 'interrupted'
                    return
                if mode == 'chat':
                    self._chat(text, model, loaded_images)
                    status = 'interrupted' if self.cancelled.is_set() else 'completed'
                else:
                    status = self._agent(mode, text, cwd, model, effort, thread_id, task_sandbox, loaded_images)
            except CancelledBeforeTurn:
                status = 'interrupted'
            except TurnStateUnknown as exc:
                status = 'unknown'
                self.emit('error', str(exc))
            except Exception as exc:
                if self.cancelled.is_set() and mode == 'chat':
                    status = 'interrupted'
                else:
                    self.emit('error', str(exc))
            finally:
                with self.active_lock:
                    self.active = None
                    self.response = None
                self.emit('done', {'mode': mode, 'status': status})
        worker = threading.Thread(target=run, daemon=True)
        with self.active_lock:
            if self.closing:
                self.active = None
                return
            self.worker = worker
            worker.start()

    def _chat(self, text, model=None, images=()):
        token = self.auth.token()
        headers = {'Authorization': 'Bearer ' + token}
        models = self._chatgpt_models(token)
        available = {entry['slug'] for entry in models}
        if model is not None and model not in available:
            raise RuntimeError('所选模型已不在当前 ChatGPT 账户列表中，请刷新模型。')
        selected = next(entry for entry in models if entry['slug'] == (model or models[0]['slug']))
        if images:
            ensure_image_supported(selected, 'input_modalities')
            if self.history_image_bytes + sum(len(data) for _, _, data in images) > MAX_CHAT_IMAGE_BYTES:
                raise RuntimeError('当前普通对话的图片累计超过 40 MiB，请开始新对话。')
        if self.cancelled.is_set():
            return
        content = text
        if images:
            content = ([{'type': 'input_text', 'text': text}] if text else [])
            content += [{'type': 'input_image', 'image_url': 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')}
                        for mime, _, data in images]
        inputs = self.history + [{'role': 'user', 'content': content}]
        response = requests.post(RESOURCE + '/responses', headers=headers,
            json={'model': model or models[0]['slug'], 'input': inputs, 'store': False, 'stream': True,
                  'instructions': ('默认用简体中文回复；如果用户要求其他语言或文字，遵从用户的要求。'
                                   '保留代码、引用和专有名称的原文。')},
            stream=True, timeout=(30, 30))
        self.response = response
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            message = chatgpt_http_error(response)
            response.close()
            raise RuntimeError(message) from exc
        answer, completed = [], False
        try:
            for line in response.iter_lines(decode_unicode=True):
                # Requests leaves SSE as bytes when the response has no declared charset.
                if isinstance(line, bytes):
                    line = line.decode('utf-8')
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
                    response_body = event.get('response') if isinstance(event.get('response'), dict) else event
                    if kind == 'error' and not isinstance(response_body.get('error'), dict):
                        response_body = {'error': response_body}
                    request_id = (getattr(response, 'headers', {}) or {}).get('x-request-id')
                    raise RuntimeError(chatgpt_error_message(body=response_body, request_id=request_id,
                                                             incomplete=kind == 'response.incomplete'))
        finally:
            response.close()
        if not completed:
            raise RuntimeError('ChatGPT 流未完整结束，请重试。')
        self.history = inputs + [{'role': 'assistant', 'content': ''.join(answer)}]
        self.history_image_bytes += sum(len(data) for _, _, data in images)

    def _agent(self, mode, text, cwd, model=None, effort=None, thread_id=None, task_sandbox=None, images=()):
        if thread_id and task_sandbox not in ('read-only', 'workspace-write'):
            raise RuntimeError('无法可靠继承历史会话的原有权限，请明确选择本次运行的只读或工作区写入权限。')
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise RuntimeError('请选择有效的工作目录。')
        token = self.auth.token() if mode == 'plan' else None
        server = AppServer(self.emit, token=token, executable=self.executable, stop_event=self.cancelled)
        server.mode = mode
        snapshots = None
        with self.active_lock:
            self.active = server
        try:
            if self.cancelled.is_set():
                raise CancelledBeforeTurn()
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
                available = self._chatgpt_models(token)
                if model is not None and model not in {entry['slug'] for entry in available}:
                    raise RuntimeError('所选模型已不在当前 ChatGPT 账户列表中，请刷新模型。')
                params['model'] = model or available[0]['slug']
                if images:
                    ensure_image_supported(next(entry for entry in available if entry['slug'] == params['model']), 'input_modalities')
            elif model:
                params['model'] = model
            key = (mode, str(directory))
            if thread_id:
                selected = server.request('thread/read', {'threadId': thread_id}).get('thread', {})
                selected_cwd = selected.get('cwd')
                if not selected_cwd or Path(selected_cwd).expanduser().resolve() != directory:
                    raise RuntimeError('所选 Codex 对话的工作目录与当前目录不一致，请先切换工作目录。')
                if selected.get('modelProvider') != 'openai':
                    raise RuntimeError('无法确认所选历史使用 OpenAI 模型提供商；不会静默切换。')
                if model is None and not selected.get('model'):
                    raise RuntimeError('历史对话未提供原模型，无法确认是否保留；请明确选择模型。')
                if effort is None and not selected.get('reasoningEffort'):
                    raise RuntimeError('历史对话未提供原推理强度，无法确认是否保留；请明确选择推理强度。')
                params['sandbox'] = task_sandbox
                started = server.request('thread/resume', {'threadId': thread_id, **params})
                expected_sandbox = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite'}[task_sandbox]
                if (started.get('sandbox') or {}).get('type') != expected_sandbox or started.get('approvalPolicy') != 'on-request':
                    raise RuntimeError('Codex 未确认所选沙箱和审批策略；未启动任务。')
                if model is None and started.get('model') != selected['model']:
                    raise RuntimeError('Codex 恢复的模型与历史模型不一致；请明确选择模型。')
                if effort is None and started.get('reasoningEffort') != selected['reasoningEffort']:
                    raise RuntimeError('Codex 恢复的推理强度与历史不一致；请明确选择推理强度。')
                self.threads[key] = started['thread']['id']
            elif key in self.threads:
                started = server.request('thread/resume', {'threadId': self.threads[key], **params})
            else:
                started = server.request('thread/start', params)
                self.threads[key] = started['thread']['id']
            thread = started['thread']
            self.emit('thread', {'mode': mode, 'thread': thread})
            if images and mode == 'task':
                selected_model = model or started.get('model')
                cursor = None
                while True:
                    query = {'limit': 100, 'includeHidden': False}
                    if cursor:
                        query['cursor'] = cursor
                    catalog = server.request('model/list', query)
                    entry = next((entry for entry in catalog.get('data', [])
                                  if (entry.get('model') or entry.get('id')) == selected_model), None)
                    if entry is not None:
                        ensure_image_supported(entry, 'inputModalities')
                        break
                    cursor = catalog.get('nextCursor')
                    if not cursor:
                        break
            # This is a separate user-level preference, not a replacement for the
            # Codex session's developer instructions. It appears in shared history.
            turn_input = [{'type': 'text', 'text':
                           '桌宠语言偏好：默认用简体中文回复；如果本轮用户要求其他语言或文字，请遵从。'
                           '保留代码、引用和专有名称的原文。'}]
            if text:
                turn_input.append({'type': 'text', 'text': text})
            if images:
                snapshots = tempfile.TemporaryDirectory(prefix='codex-pet-images-')
                for index, (_, suffix, data) in enumerate(images):
                    snapshot = Path(snapshots.name) / (str(index) + suffix)
                    snapshot.write_bytes(data)
                    snapshot.chmod(0o600)
                    turn_input.append({'type': 'localImage', 'path': str(snapshot)})
            start = {'threadId': thread['id'], 'input': turn_input}
            if model:
                start['model'] = model
            if effort:
                start['effort'] = effort
            if mode == 'plan':
                start['collaborationMode'] = {'mode': 'plan', 'settings': {'model': params['model'], 'developer_instructions': None}}
                start['sandboxPolicy'] = {'type': 'readOnly', 'networkAccess': False}
                start['approvalPolicy'] = 'never'
            if self.cancelled.is_set():
                raise CancelledBeforeTurn()
            try:
                turn = server.request('turn/start', start)['turn']
            except TurnStateUnknown:
                raise
            except RuntimeError as exc:
                if images:
                    raise RuntimeError('图片任务无法启动：' + str(exc)) from exc
                raise
            interrupt_sent = False
            interrupt_deadline = None
            while True:
                result = server.completed_turns.pop(turn['id'], None)
                if result is not None:
                    turn_status = result.get('status')
                    if turn_status in ('completed', 'interrupted'):
                        return turn_status
                    if images:
                        raise RuntimeError('图片任务未完成，请检查模型的图片能力、账户授权或额度：' + str(turn_status))
                    raise RuntimeError('Codex 任务未完成：' + str(turn_status))
                if self.cancelled.is_set() and not interrupt_sent:
                    interrupt_sent = True
                    interrupt_deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
                    try:
                        server.request('turn/interrupt', {'threadId': thread['id'], 'turnId': turn['id']})
                    except RuntimeError as exc:
                        # Completion can race with the interrupt response.
                        if turn['id'] not in server.completed_turns:
                            raise TurnStateUnknown('停止请求未得到确认；任务执行状态未知。') from exc
                    # request() can consume turn/completed before the response.
                    # Recheck the cache before waiting for any further events.
                    continue
                timeout = 120 if interrupt_deadline is None else max(0, interrupt_deadline - time.monotonic())
                try:
                    server.handle(server.next_event(timeout))
                except RuntimeError as exc:
                    if self.cancelled.is_set():
                        raise TurnStateUnknown('未收到任务的最终状态；请检查 Codex 会话。') from exc
                    raise
        finally:
            try:
                server.close()
            finally:
                if snapshots is not None:
                    snapshots.cleanup()

    def cancel(self):
        self.cancelled.set()
        with self.active_lock:
            active = self.active
            response = self.response
        if isinstance(active, AppServer):
            active.wake()
        if response is not None:
            threading.Thread(target=response.close, daemon=True).start()

    def approve(self, request_id, decision):
        with self.active_lock:
            active = self.active
            if self.cancelled.is_set():
                self.emit('error', '任务正在停止，审批请求已失效。')
                return
        if isinstance(active, AppServer):
            try:
                active.approve(request_id, decision)
            except Exception as exc:
                self.emit('error', str(exc))

    def close(self):
        with self.active_lock:
            self.closing = True
            history_processes = list(self.history_processes)
        # These are only short-lived servers created for explicit history reads.
        # Terminate them together and cap the total UI-thread wait.
        for process in history_processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + .5
        for process in history_processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
        for process in history_processes:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        self.cancel()
        with self.active_lock:
            worker = self.worker
        if not worker or worker is threading.current_thread():
            return
        # aboutToQuit is the last chance to reap the App Server and let its
        # worker remove private image snapshots before the process exits.
        worker.join(1.0)
        if not worker.is_alive():
            return
        with self.active_lock:
            active = self.active
        if isinstance(active, AppServer) and active.process.poll() is None:
            try:
                active.process.terminate()
            except ProcessLookupError:
                pass
        worker.join(.75)
        if worker.is_alive() and isinstance(active, AppServer) and active.process.poll() is None:
            try:
                active.process.kill()
            except ProcessLookupError:
                pass
            worker.join(.75)
