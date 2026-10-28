"""Asynchronous Codex Pet conversation and account bridge.

The callback receives (event, data) on a worker thread; UI must marshal it to
its event loop. Only official ChatGPT OAuth and Codex app-server are used.
"""
import json
import hashlib
from collections import OrderedDict
from contextlib import contextmanager
import base64
import io
import os
import platform
import re
import shlex
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

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
MAX_APPROVAL_PATCH_ITEMS = 16
MAX_APPROVAL_PATCH_CHARS = 256_000
# Local loopback verified this exact Linux x86-64 codex-cli 0.162.0-alpha.17.2 binary.
VERIFIED_STDIN_BINARIES = {"73b73b048546993298234a295de01ef2424e7a8b29def9d0ecb1ca7cb77500d4"}


def stdin_approval_details(params):
    """Validate only the current request's complete, shell-quoted stdin preview."""
    if not isinstance(params, dict) or params.get('kind') != 'writeStdin':
        return None
    for name in ('threadId', 'turnId', 'itemId', 'approvalId', 'cwd', 'command'):
        value = params.get(name)
        if not isinstance(value, str) or not value or len(value) > MAX_APPROVAL_PATCH_CHARS:
            return None
    if params.get('networkApprovalContext') is not None:
        return None
    try:
        parts = shlex.split(params['command'])
    except ValueError:
        return None
    if (len(parts) != 4 or parts[:2] != ['write_stdin', '--session-id']
            or not parts[2].isascii() or not parts[2].isdigit() or not parts[3]
            or '\0' in parts[3]):
        return None
    try:
        snapshot = json.dumps(params, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError):
        return None
    return snapshot if len(snapshot) <= MAX_APPROVAL_PATCH_CHARS else None


PET_LANGUAGE_PREFERENCE = ('桌宠语言偏好：默认用简体中文回复；如果本轮用户要求其他语言或文字，请遵从。'
                           '保留代码、引用和专有名称的原文。')

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


def _confirm_handoff_turn(server, turn_id):
    server.handoff_turn_id = turn_id
    pending = getattr(server, 'handoff_pending', None)
    if getattr(server, 'handoff_overflow', False):
        server.emit('handoff_unavailable', {'request_id': server.handoff_request_id})
    elif pending and turn_id in pending:
        server.emit('handoff_reply', pending[turn_id])
    server.handoff_pending = None


class AppServer:
    def __init__(self, emit, token=None, executable=None, stop_event=None, process_factory=None):
        self.emit = emit
        self.server_id = uuid.uuid4().hex
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
        factory = process_factory or subprocess.Popen
        self.process = factory(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
        self.events = queue.Queue()
        self.write_lock = threading.Lock()
        self.counter = 0
        self.pending_approvals = {}
        self.file_change_patches = OrderedDict()
        self.command_items = OrderedDict()
        self.pending_command_snapshots = {}
        self.pending_stdin_snapshots = {}
        self.pending_patch_snapshots = {}
        self.event_lock = threading.RLock()
        self.queued_server_requests = set()
        self.resolved_queued_requests = set()
        self.queued_request_details = {}
        self.queued_request_serial = 0
        self.read_item_details = OrderedDict()
        self.stale_queued_requests = set()
        self.reader_closed = False
        self.completed_turns = {}
        self.mode = None
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request('initialize', {'clientInfo': {'name': 'codex_pet', 'title': 'Codex Pet', 'version': '2.28.0'}, 'capabilities': {'experimentalApi': True}})
            self.write({'method': 'initialized', 'params': {}})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                method = event.get('method')
                params = event.get('params') or {}
                if method == 'serverRequest/resolved':
                    # Resolve immediately so a queued GUI event cannot approve it.
                    self.handle(event)
                    continue
                with self.event_lock:
                    if 'id' in event and method:
                        key = (params.get('threadId'), event['id'])
                        self.queued_request_serial += 1
                        token = self.queued_request_serial
                        event['_pet_queue_token'] = token
                        self.queued_server_requests.add(token)
                        item_key = self._patch_key(params)
                        snapshot = self.read_item_details.get((method, item_key))
                        self.queued_request_details[token] = (key, method, params, snapshot)
                    elif method in ('item/fileChange/patchUpdated', 'item/started', 'item/completed'):
                        item = params.get('item') if method != 'item/fileChange/patchUpdated' else None
                        kind = item.get('type') if isinstance(item, dict) else 'fileChange'
                        request_method = {'fileChange': 'item/fileChange/requestApproval',
                                          'commandExecution': 'item/commandExecution/requestApproval'}.get(kind)
                        item_id = item.get('id') if isinstance(item, dict) else None
                        item_key = self._patch_key(params, item_id)
                        if request_method and item_key is not None:
                            terminal = method == 'item/completed'
                            cache_key = (request_method, item_key)
                            if terminal:
                                self.read_item_details.pop(cache_key, None)
                                updated = None
                            elif kind == 'commandExecution':
                                updated = self._validated_command(item)
                            else:
                                changes = params.get('changes') if item is None else item.get('changes')
                                updated = self._validated_patch(changes)
                            if not terminal:
                                self.read_item_details[cache_key] = updated
                                self.read_item_details.move_to_end(cache_key)
                                while len(self.read_item_details) > MAX_APPROVAL_PATCH_ITEMS:
                                    self.read_item_details.popitem(last=False)
                            for token, (_, queued_method, request_params, snapshot) in self.queued_request_details.items():
                                if (queued_method == request_method and self._patch_key(request_params) == item_key
                                        and (terminal or snapshot != updated)):
                                    (self.resolved_queued_requests if terminal else self.stale_queued_requests).add(token)
                            snapshots = (self.pending_command_snapshots if kind == 'commandExecution'
                                         else self.pending_patch_snapshots)
                            for request_id, (pending_method, request_params) in list(self.pending_approvals.items()):
                                if (pending_method == request_method and self._patch_key(request_params) == item_key
                                        and (terminal or snapshots.get(request_id) != updated)):
                                    if not terminal:
                                        self._deny_changed_request(request_id, pending_method, request_params)
                                    self._invalidate_approval(request_id, request_params.get('threadId'))
                    elif method == 'turn/completed':
                        thread_id = params.get('threadId')
                        turn = params.get('turn') or {}
                        turn_id = turn.get('id') if isinstance(turn, dict) else None
                        if thread_id and turn_id:
                            for cache_key in list(self.read_item_details):
                                if cache_key[1][:2] == (thread_id, turn_id):
                                    del self.read_item_details[cache_key]
                            for token, (_, _, request_params, _) in self.queued_request_details.items():
                                if (request_params.get('threadId'), request_params.get('turnId')) == (thread_id, turn_id):
                                    self.resolved_queued_requests.add(token)
                            for request_id, (_, request_params) in list(self.pending_approvals.items()):
                                if (request_params.get('threadId'), request_params.get('turnId')) == (thread_id, turn_id):
                                    self._invalidate_approval(request_id, thread_id)
                self.events.put(event)
        finally:
            with self.event_lock:
                self.reader_closed = True
                self.invalidate_approvals()
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
                if (self.stop_event and self.stop_event.is_set()
                        and method not in ('turn/start', 'turn/interrupt')):
                    raise CancelledBeforeTurn()
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

    @staticmethod
    def _patch_key(params, item_id=None):
        key = (params.get('threadId'), params.get('turnId'), item_id if item_id is not None else params.get('itemId'))
        return key if all(isinstance(value, str) and value for value in key) else None

    @staticmethod
    def _validated_patch(changes):
        # Invalid or oversized changes are marked unavailable, never partially presented as complete.
        checked = []
        size = 0
        if isinstance(changes, list) and 0 < len(changes) <= 1000:
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get('path'), str) or not isinstance(change.get('diff'), str):
                    checked = None
                    break
                kind = change.get('kind')
                if not isinstance(kind, dict) or kind.get('type') not in ('add', 'delete', 'update'):
                    checked = None
                    break
                move_path = kind.get('move_path')
                if move_path is not None and not isinstance(move_path, str):
                    checked = None
                    break
                size += len(change['path']) + len(change['diff']) + len(move_path or '') + 16
                if size > MAX_APPROVAL_PATCH_CHARS:
                    checked = None
                    break
                safe_kind = {'type': kind['type']}
                if move_path is not None:
                    safe_kind['move_path'] = move_path
                checked.append({'path': change['path'], 'kind': safe_kind, 'diff': change['diff']})
        else:
            checked = None
        return checked

    @staticmethod
    def _validated_command(item):
        command, cwd = item.get('command'), item.get('cwd')
        if (isinstance(command, str) and command and len(command) <= MAX_APPROVAL_PATCH_CHARS
                and isinstance(cwd, str) and cwd and len(cwd) <= MAX_APPROVAL_PATCH_CHARS):
            return {'command': command, 'cwd': cwd}
        return None

    def _remember_patch(self, key, changes):
        if key is None:
            return
        checked = self._validated_patch(changes)
        patches = getattr(self, 'file_change_patches', None)
        if patches is None:
            patches = self.file_change_patches = OrderedDict()
        patches[key] = checked
        patches.move_to_end(key)
        while len(patches) > MAX_APPROVAL_PATCH_ITEMS:
            patches.popitem(last=False)

    def _stdin_protocol_verified(self):
        """Identify the running process, not a potentially replaced executable path."""
        if not hasattr(self, "_stdin_binary_verified"):
            try:
                with open(f"/proc/{self.process.pid}/exe", "rb") as executable:
                    digest = hashlib.file_digest(executable, "sha256").hexdigest()
                self._stdin_binary_verified = digest in VERIFIED_STDIN_BINARIES
            except (OSError, AttributeError):
                self._stdin_binary_verified = False
        return self._stdin_binary_verified

    def _handoff_message(self, params, item):
        """Buffer pre-response items until the new turn ID is known."""
        request_id = getattr(self, 'handoff_request_id', None)
        turn_id = params.get('turnId')
        text, phase = item.get('text'), item.get('phase')
        if (not request_id or params.get('threadId') != getattr(self, 'handoff_thread_id', None)
                or not isinstance(turn_id, str) or not turn_id or not isinstance(text, str)
                or not text.strip() or phase not in ('final_answer', None)):
            return
        marker = '【前文已截断】\n'
        if len(text) > 8000:
            text = marker + text[-(8000 - len(marker)):]
        data = {'text': text, 'phase': phase, 'request_id': request_id}
        if getattr(self, 'handoff_overflow', False):
            return
        current_turn = getattr(self, 'handoff_turn_id', None)
        if current_turn is not None:
            if turn_id == current_turn:
                self.emit('handoff_reply', data)
            return
        pending = getattr(self, 'handoff_pending', None)
        if pending is None or getattr(self, 'handoff_overflow', False):
            return
        previous = pending.get(turn_id)
        if previous and previous['phase'] == 'final_answer' and phase is None:
            return
        if turn_id not in pending and len(pending) >= 64:
            pending.clear()
            self.handoff_overflow = True
            return
        pending[turn_id] = data

    def handle(self, event):
        lock = getattr(self, 'event_lock', None)
        if lock is None:  # Isolated unit test instances do not start a server.
            lock = self.event_lock = threading.RLock()
        with lock:
            return self._handle(event)

    def _handle(self, event):
        method = event.get('method', '')
        params = event.get('params') or {}
        if 'id' in event and method:
            request_id = event['id']
            key = (params.get('threadId'), request_id)
            token = event.pop('_pet_queue_token', None)
            getattr(self, 'queued_server_requests', set()).discard(token)
            getattr(self, 'queued_request_details', {}).pop(token, None)
            stale = getattr(self, 'stale_queued_requests', set())
            was_stale = token in stale
            stale.discard(token)
            resolved = getattr(self, 'resolved_queued_requests', set())
            if token in resolved:
                resolved.discard(token)
                return method
            if (getattr(self, 'reader_closed', False) or
                    getattr(self, 'stop_event', None) and self.stop_event.is_set()):
                return method
            if was_stale:
                self._deny_changed_request(request_id, method, params)
                return method
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
                    payload = {'id': request_id, 'method': method, 'params': params}
                    if getattr(self, 'server_id', None):
                        payload['serverId'] = self.server_id
                    if method == 'item/commandExecution/requestApproval':
                        item = getattr(self, 'command_items', {}).get(self._patch_key(params))
                        payload['command_item'] = dict(item) if item else None
                        snapshots = getattr(self, 'pending_command_snapshots', None)
                        if snapshots is None:
                            snapshots = self.pending_command_snapshots = {}
                        snapshots[request_id] = dict(item) if item else None
                        if params.get('kind') == 'writeStdin':
                            snapshots = getattr(self, 'pending_stdin_snapshots', None)
                            if snapshots is None:
                                snapshots = self.pending_stdin_snapshots = {}
                            verified = self._stdin_protocol_verified()
                            snapshots[request_id] = stdin_approval_details(params) if verified else None
                            payload['stdin_protocol_verified'] = verified
                    if method == 'item/fileChange/requestApproval':
                        key = self._patch_key(params)
                        payload['file_changes'] = getattr(self, 'file_change_patches', {}).get(key)
                        snapshots = getattr(self, 'pending_patch_snapshots', None)
                        if snapshots is None:
                            snapshots = self.pending_patch_snapshots = {}
                        snapshots[request_id] = payload['file_changes']
                    self.emit('approval', payload)
            else:
                # Do not guess response shapes or grant unknown requests.
                self.write({'id': request_id, 'error': {'code': -32601, 'message': 'Unsupported request'}})
                self.emit('status', '不支持的 Codex 请求已拒绝：' + method)
        elif method == 'serverRequest/resolved':
            key = (params.get('threadId'), params.get('requestId'))
            for token, (queued_key, _, _, _) in getattr(self, 'queued_request_details', {}).items():
                if queued_key == key:
                    self.resolved_queued_requests.add(token)
            pending = self.pending_approvals.get(key[1])
            if pending and pending[1].get('threadId') == key[0]:
                self._invalidate_approval(key[1], key[0])
        elif method == 'turn/completed':
            turn = params.get('turn', {})
            self.completed_turns[turn.get('id')] = turn
            thread_id, turn_id = params.get('threadId'), turn.get('id')
            for request_id, (_, approval) in list(self.pending_approvals.items()):
                if approval.get('threadId') == thread_id and approval.get('turnId') == turn_id:
                    self._invalidate_approval(request_id, thread_id)
            commands = getattr(self, 'command_items', {})
            for key in list(commands):
                if key[:2] == (thread_id, turn_id):
                    del commands[key]
            patches = getattr(self, 'file_change_patches', {})
            for key in list(patches):
                if key[:2] == (thread_id, turn_id):
                    del patches[key]
        elif method == 'item/fileChange/patchUpdated':
            key = self._patch_key(params)
            if key in getattr(self, 'file_change_patches', {}):
                self._remember_patch(key, params.get('changes'))
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
            if kind == 'commandExecution':
                key = self._patch_key(params, item.get('id'))
                commands = getattr(self, 'command_items', None)
                if commands is None:
                    commands = self.command_items = OrderedDict()
                if key is not None and method == 'item/started':
                    command = self._validated_command(item)
                    if command is not None:
                        commands[key] = command
                        commands.move_to_end(key)
                        while len(commands) > MAX_APPROVAL_PATCH_ITEMS:
                            commands.popitem(last=False)
                    else:
                        # An invalid replacement must not leave an older command reviewable.
                        commands.pop(key, None)
                elif key is not None and method == 'item/completed':
                    commands.pop(key, None)
            if kind == 'fileChange':
                key = self._patch_key(params, item.get('id'))
                if method == 'item/started':
                    self._remember_patch(key, item.get('changes'))
                else:
                    getattr(self, 'file_change_patches', {}).pop(key, None)
            if kind == 'agentMessage' and method == 'item/completed':
                self._handoff_message(params, item)
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

    def _deny_changed_request(self, request_id, method, params):
        """Reject an approval whose item changed after the preview was read."""
        allowed = params.get('availableDecisions') if method == 'item/commandExecution/requestApproval' else None
        denial = next((choice for choice in ('decline', 'cancel')
                       if allowed is None or isinstance(allowed, list) and choice in allowed), None)
        if denial is None:
            self.write({'id': request_id, 'error': {'code': -32601,
                        'message': 'Item changed; no supported denial decision'}})
        else:
            self.write({'id': request_id, 'result': {'decision': denial}})
        self.emit('error', '审批期间操作详情发生变化，已拒绝；请重新检查。')

    def _invalidate_approval(self, request_id, thread_id):
        self.pending_approvals.pop(request_id, None)
        getattr(self, 'pending_patch_snapshots', {}).pop(request_id, None)
        getattr(self, 'pending_command_snapshots', {}).pop(request_id, None)
        getattr(self, 'pending_stdin_snapshots', {}).pop(request_id, None)
        data = {'id': request_id, 'threadId': thread_id}
        if getattr(self, 'server_id', None):
            data['serverId'] = self.server_id
        self.emit('approval_resolved', data)

    def invalidate_approvals(self):
        with self.event_lock:
            for request_id, (_, params) in list(self.pending_approvals.items()):
                self._invalidate_approval(request_id, params.get('threadId'))

    def wake(self):
        """Wake the sole event consumer after a stop request."""
        self.events.put({'_pet_wake': True})

    def approve(self, request_id, decision):
        lock = getattr(self, 'event_lock', None)
        if lock is None:
            lock = self.event_lock = threading.RLock()
        with lock:
            return self._approve(request_id, decision)

    def _approve(self, request_id, decision):
        if request_id not in self.pending_approvals:
            raise RuntimeError('审批请求已失效。')
        method, params = self.pending_approvals[request_id]
        if method == 'item/commandExecution/requestApproval' and decision == 'accept':
            snapshots = getattr(self, 'pending_command_snapshots', {})
            snapshot = snapshots.get(request_id)
            current = getattr(self, 'command_items', {}).get(self._patch_key(params))
            if snapshot != current:
                allowed = params.get('availableDecisions')
                denial = next((d for d in ('decline', 'cancel')
                               if allowed is None or isinstance(allowed, list) and d in allowed), None)
                if denial is None:
                    self.write({'id': request_id, 'error': {'code': -32601,
                                'message': 'Command changed; no supported denial decision'}})
                else:
                    self.write({'id': request_id, 'result': {'decision': denial}})
                del self.pending_approvals[request_id]
                snapshots.pop(request_id, None)
                getattr(self, 'pending_stdin_snapshots', {}).pop(request_id, None)
                self.emit('error', '审批期间命令内容发生变化，已拒绝；请重新检查。')
                return
            network = params.get('networkApprovalContext')
            if params.get('kind') == 'writeStdin':
                shown = getattr(self, 'pending_stdin_snapshots', {}).get(request_id)
                current_preview = stdin_approval_details(params)
                if shown and shown != current_preview:
                    self._deny_changed_request(request_id, method, params)
                    self._invalidate_approval(request_id, params.get('threadId'))
                    return
                reviewable = bool(shown and shown == current_preview)
            elif params.get('kind', 'command') != 'command':
                reviewable = False
            elif network is not None:
                reviewable = (isinstance(network, dict) and isinstance(network.get('host'), str)
                              and bool(network['host']) and isinstance(network.get('protocol'), str)
                              and bool(network['protocol']))
            else:
                item = getattr(self, 'command_items', {}).get(self._patch_key(params)) or {}
                # A subcommand callback can share its parent item; its command
                # must come from the approval itself, not the parent preview.
                item_command = item.get('command') if not params.get('approvalId') else None
                has_command = any(isinstance(value, str) and value
                                  for value in (params.get('command'), item_command))
                has_cwd = any(isinstance(value, str) and value
                              for value in (params.get('cwd'), item.get('cwd')))
                reviewable = has_command and has_cwd
            if not reviewable:
                raise ValueError('审批详情不完整，不能允许。')
        if method == 'item/fileChange/requestApproval' and decision == 'accept':
            snapshots = getattr(self, 'pending_patch_snapshots', {})
            snapshot = snapshots.get(request_id)
            current = getattr(self, 'file_change_patches', {}).get(self._patch_key(params))
            if snapshot != current:
                self.write({'id': request_id, 'result': {'decision': 'decline'}})
                del self.pending_approvals[request_id]
                snapshots.pop(request_id, None)
                self.emit('error', '审批期间文件变更内容发生变化，已拒绝；请重新检查。')
                return
            if not snapshot:
                raise ValueError('文件差异不可用，不能允许。')
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
        getattr(self, 'pending_patch_snapshots', {}).pop(request_id, None)
        getattr(self, 'pending_command_snapshots', {}).pop(request_id, None)
        getattr(self, 'pending_stdin_snapshots', {}).pop(request_id, None)

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
        self.pending_processes = set()
        self.history = []
        self.history_image_bytes = 0
        self.threads = {}
        # Only threads started by this backend instance can use its original
        # default reasoning semantics. Selected external history stays strict.
        self.created_threads = {}

    def _async(self, operation, error_event='error'):
        def run():
            try:
                operation()
            except Exception as exc:
                self.emit(error_event, str(exc))
        threading.Thread(target=run, daemon=True).start()

    def _tracked_process_factory(self, registered):
        def spawn(*args, **kwargs):
            # Close and spawning must be atomic: otherwise a server can appear
            # just after close() snapshots the set of children to terminate.
            with self.active_lock:
                if self.closing:
                    raise RuntimeError('应用正在退出。')
                process = subprocess.Popen(*args, **kwargs)
                self.pending_processes.add(process)
                registered.append(process)
                return process
        return spawn

    @contextmanager
    def _query_server(self):
        """Track a short-lived query server even while it initializes."""
        registered = []
        server = None
        try:
            server = AppServer(self.emit, executable=self.executable,
                               process_factory=self._tracked_process_factory(registered))
            yield server
        finally:
            try:
                if server is not None:
                    server.close()
            finally:
                with self.active_lock:
                    for process in registered:
                        self.pending_processes.discard(process)

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
            with self._query_server() as server:
                account = server.request('account/read', {'refreshToken': False}).get('account')
                self.emit('account', {'source': 'Codex', 'account': account})
        self._async(run, 'account_error')

    def limits(self):
        def run():
            with self._query_server() as server:
                account = server.request('account/read', {'refreshToken': False}).get('account')
                if not account or account.get('type') != 'chatgpt':
                    raise RuntimeError('Codex 未以 ChatGPT 账户登录；不会使用 API key。')
                result = server.request('account/rateLimits/read', {})
            self.emit('limits', result)
        self._async(run, 'limits_error')

    def _check_identity(self, account=None):
        chat = self.auth.account()
        if not chat.get('signedIn'):
            self.emit('status', 'ChatGPT 独立授权未登录，无法核对它与 Codex 是否为同一账户。')
            return
        if account is None:
            try:
                with self._query_server() as server:
                    account = server.request('account/read', {'refreshToken': False}).get('account')
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
                    with self._query_server() as server:
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
            with self._query_server() as server:
                account = server.request('account/read', {'refreshToken': False}).get('account')
                if not account or account.get('type') != 'chatgpt':
                    raise RuntimeError('Codex 未以 ChatGPT 账户登录；不会使用 API key。')
                params = {'limit': 50, 'sourceKinds': ['cli', 'vscode', 'appServer'],
                          'sortKey': 'updated_at'}
                if cursor:
                    params['cursor'] = cursor
                result = server.request('thread/list', params)
            self.emit('codex_threads', result)
        self._async(run, 'codex_threads_error')

    def codex_thread(self, thread_id):
        """Read history only after the user explicitly chooses that session."""
        def run():
            if not thread_id:
                raise RuntimeError('请选择 Codex 对话。')
            with self._query_server() as server:
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
            self.emit('codex_thread', result)
        self._async(run, 'codex_thread_error')

    def new_thread(self, mode, cwd):
        """Forget the active local mapping; existing Codex history is retained."""
        if mode in ('plan', 'task') and cwd:
            key = (mode, str(Path(cwd).expanduser().resolve()))
            self.threads.pop(key, None)
            self.created_threads.pop(key, None)
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
        request_id = uuid.uuid4().hex
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
                    status = self._agent(mode, text, cwd, model, effort, thread_id, task_sandbox, loaded_images,
                                         request_id=request_id)
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
        return request_id

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

    @staticmethod
    def _resume_thread(server, params):
        try:
            return server.request('thread/resume', params)
        except RpcError as exc:
            # A second app-server can read an open session but cannot write it
            # until the first writer closes. Match only the observed error for
            # the selected thread; leave all unrelated RPC failures unchanged.
            thread_id = params['threadId']
            if exc.code == -32600 and str(exc) == f'thread {thread_id} already has an active writer':
                raise RuntimeError('该 Codex 对话正被其他客户端占用。请关闭占用此对话的客户端后重试；草稿会保留。') from exc
            raise

    def _agent(self, mode, text, cwd, model=None, effort=None, thread_id=None, task_sandbox=None, images=(),
               request_id=None):
        if thread_id and task_sandbox not in ('read-only', 'workspace-write'):
            raise RuntimeError('无法可靠继承历史会话的原有权限，请明确选择本次运行的只读或工作区写入权限。')
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise RuntimeError('请选择有效的工作目录。')
        token = self.auth.token() if mode == 'plan' else None
        # Track the process before initialize can block. Once initialized,
        # transfer it to the active turn under the same lock used by close().
        registered = []
        try:
            server = AppServer(self.emit, token=token, executable=self.executable,
                               stop_event=self.cancelled,
                               process_factory=self._tracked_process_factory(registered))
        except Exception:
            with self.active_lock:
                for process in registered:
                    self.pending_processes.discard(process)
            raise
        server.mode = mode
        snapshots = None
        with self.active_lock:
            for process in registered:
                self.pending_processes.discard(process)
            self.active = server
            closing = self.closing
        try:
            if closing or self.cancelled.is_set():
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
                own_thread = self.created_threads.get(key) == thread_id
                if effort is None and not selected.get('reasoningEffort') and not own_thread:
                    raise RuntimeError('历史对话未提供原推理强度，无法确认是否保留；请明确选择推理强度。')
                params['sandbox'] = task_sandbox
                started = self._resume_thread(server, {'threadId': thread_id, **params})
                expected_sandbox = {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite'}[task_sandbox]
                if (started.get('sandbox') or {}).get('type') != expected_sandbox or started.get('approvalPolicy') != 'on-request':
                    raise RuntimeError('Codex 未确认所选沙箱和审批策略；未启动任务。')
                if model is None and started.get('model') != selected['model']:
                    raise RuntimeError('Codex 恢复的模型与历史模型不一致；请明确选择模型。')
                if (effort is None and selected.get('reasoningEffort')
                        and started.get('reasoningEffort') != selected['reasoningEffort']):
                    raise RuntimeError('Codex 恢复的推理强度与历史不一致；请明确选择推理强度。')
                self.threads[key] = started['thread']['id']
            elif key in self.threads:
                started = self._resume_thread(server, {'threadId': self.threads[key], **params})
            else:
                started = server.request('thread/start', params)
                self.threads[key] = started['thread']['id']
                self.created_threads[key] = started['thread']['id']
            thread = started['thread']
            server.handoff_request_id = request_id
            server.handoff_thread_id = thread['id']
            self.emit('thread', {'mode': mode, 'thread': thread, 'request_id': request_id})
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
            turn_input = [{'type': 'text', 'text': PET_LANGUAGE_PREFERENCE}]
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
            server.handoff_pending = {}
            server.handoff_overflow = False
            server.handoff_turn_id = None
            try:
                turn = server.request('turn/start', start)['turn']
                _confirm_handoff_turn(server, turn['id'])
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
            active.invalidate_approvals()
            active.wake()
        if response is not None:
            threading.Thread(target=response.close, daemon=True).start()

    def approve(self, request_id, decision, server_id=None):
        with self.active_lock:
            active = self.active
            if self.cancelled.is_set():
                self.emit('error', '任务正在停止，审批请求已失效。')
                return
            if not isinstance(active, AppServer) or not server_id or active.server_id != server_id:
                self.emit('error', '审批请求属于已结束的任务。')
                return
            try:
                active.approve(request_id, decision)
            except Exception as exc:
                self.emit('error', str(exc))

    def approval_is_pending(self, request_id, thread_id, server_id):
        with self.active_lock:
            active = self.active
            if (self.cancelled.is_set() or not isinstance(active, AppServer)
                    or not server_id or active.server_id != server_id):
                return False
            with active.event_lock:
                pending = active.pending_approvals.get(request_id)
                return bool(pending and pending[1].get('threadId') == thread_id)

    def close(self):
        with self.active_lock:
            self.closing = True
            pending_processes = list(self.pending_processes)
        self.cancel()
        # Only this backend's query servers and initializing turn servers are
        # registered; never touch unrelated Codex processes.
        # Terminate them together and cap the total UI-thread wait.
        for process in pending_processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + .5
        for process in pending_processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
        for process in pending_processes:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
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
