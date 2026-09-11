"""Current-account HTTP session provider, with Windows user-scoped DPAPI storage.

Browser bootstrap supplies authentication context, never a captured send body.
Normal prepare/check operations do not import or start a browser. No automatic
refresh, retries, target registration or sends. An expired session needs a new
explicit bootstrap. The local 12-hour age policy is not a platform expiry claim.
"""
import base64
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

import runtime
import uid_bootstrap
import uid_protocol as wire
import uid_transport

MAX_AGE = 12 * 3600
MAGIC = b'CLUBOPS-UID-DPAPI-1\n'
CONTEXT_FIELDS = {3, 4, 5, 6, 7, 9, 11, 14, 15, 18, 21, 22}
HTTP_HEADERS = {'accept', 'accept-language', 'content-type', 'cookie', 'origin', 'priority',
                'referer', 'sec-ch-ua', 'sec-ch-ua-mobile', 'sec-ch-ua-platform',
                'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site', 'user-agent'}


def vault_path():
    return runtime.data_dir() / 'private' / 'uid-http' / 'session.dpapi'


def crypt(raw, *, decrypt=False):
    if sys.platform != 'win32' or not isinstance(raw, bytes) or not 0 < len(raw) <= 131072:
        raise ValueError('本机会话保护仅支持当前 Windows 用户；没有明文回退')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; deliberately no LOCAL_MACHINE flag.
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
            raise ValueError('本机会话无法加密或解密；请在当前 Windows 用户下重新准备会话')
        if not 0 < destination.size <= 131072:
            raise ValueError('本机会话保护结果超出上限')
        return ctypes.string_at(destination.data, destination.size)
    finally:
        if destination.data:
            ctypes.memset(destination.data, 0, destination.size)
            kernel.LocalFree(destination.data)
        ctypes.memset(buffer, 0, len(raw))


def save(value, path):
    """Caller holds the directory lock. Only ciphertext is ever written."""
    encrypted = MAGIC + crypt(json.dumps(value, separators=(',', ':')).encode())
    path = Path(path)
    if path.is_symlink():
        raise ValueError('会话文件不能是符号链接')
    temporary = path.with_name('.session-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('会话文件不能是符号链接')
    with path.open('rb') as stream:
        encrypted = stream.read(131073)
    if len(encrypted) > 131072 or not encrypted.startswith(MAGIC):
        raise ValueError('本机会话文件无效；请重新准备会话')
    return validate(json.loads(crypt(encrypted[len(MAGIC):], decrypt=True)))


def header_value(value, maximum=16384):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('会话元数据无效')
    value.encode('latin-1')
    return value


def session_cookie(raw):
    """SESSION_AUTH uses the existing login pair, not unrelated page cookies.

    Preserve both actual values; do not fabricate a sessionid_ss value, choose
    between duplicate identities, or forward tracking/UI cookies to IM.
    """
    header_value(raw)
    selected = {}
    for part in raw.split(';'):
        name, separator, value = part.strip().partition('=')
        if name not in ('sessionid', 'sessionid_ss'):
            continue
        if (not separator or name in selected or not value or len(value) > 8192
                or not value.isascii() or any(ord(c) <= 32 or ord(c) >= 127 or c in '\",;\\' for c in value)):
            raise ValueError('登录 Cookie 缺失或不明确；请重新准备会话')
        selected[name] = value
    if set(selected) != {'sessionid', 'sessionid_ss'}:
        raise ValueError('需要当前账号的 sessionid 与 sessionid_ss')
    return '; '.join(name + '=' + selected[name] for name in ('sessionid', 'sessionid_ss'))


def validate(value):
    keys = {'format', 'sender_uid', 'account', 'captured_at', 'sequence', 'context', 'identity_cookie', 'headers'}
    if not isinstance(value, dict) or not keys <= set(value) or set(value) - keys - {'im_verified'} or value['format'] != 'clubops-uid-session-1':
        raise ValueError('本机会话结构无效')
    value.setdefault('im_verified', False)
    if type(value['im_verified']) is not bool:
        raise ValueError('本机会话验证状态无效')
    wire.numeric_uid(value['sender_uid'])
    uid_bootstrap.account(value['account'])
    age = value['captured_at']
    if type(age) not in (int, float) or not math.isfinite(age) or not -60 <= time.time() - age <= MAX_AGE:
        raise ValueError('本机会话超过本地复用时间；请重新准备会话')
    if type(value['sequence']) is not int or not 0 <= value['sequence'] < 2**31 - 2:
        raise ValueError('会话序列号无效')
    header_value(value['identity_cookie'])
    headers = value['headers']
    if not isinstance(headers, dict) or set(headers) - HTTP_HEADERS or not {'cookie', 'user-agent'} <= set(headers):
        raise ValueError('会话 HTTP 首部无效')
    for item in headers.values():
        header_value(item)
    session_cookie(headers['cookie'])
    if headers.get('origin', 'https://www.douyin.com') != 'https://www.douyin.com':
        raise ValueError('会话来源无效')
    context = wire.decode(base64.b64decode(value['context'], validate=True))
    if set(context) != CONTEXT_FIELDS or wire.text(context, 4) or wire.one(context, 18, 0) != 1 or any(kind not in (0, 2) for items in context.values() for kind, _ in items):
        raise ValueError('当前 IM 认证上下文不受支持；需要重新核对协议')
    for number, items in context.items():
        if number != 15 and len(items) != 1:
            raise ValueError('IM 认证字段重复')
    return value


def from_capture(value, identity):
    root = wire.decode(base64.b64decode(value['payload'], validate=True))
    if set(root) != CONTEXT_FIELDS | {1, 2, 8} or wire.one(root, 1, 0) != 1001:
        raise ValueError('需要当前账号的只读会话查询认证上下文')
    body = wire.decode(wire.one(root, 8, 2))
    if set(body) != {1000}:
        raise ValueError('只读会话查询结构无效')
    context = b''.join(wire.field(n, item) for n, fields in root.items() if n in CONTEXT_FIELDS
                       for kind, item in fields if kind in (0, 2))
    if any(kind not in (0, 2) for fields in root.values() for kind, _ in fields):
        raise ValueError('IM 上下文类型不受支持')
    headers = dict(value['headers'])
    headers['cookie'] = session_cookie(headers.get('cookie'))
    return validate(dict(format='clubops-uid-session-1', sender_uid=identity['sender_uid'],
                         account=identity['expected_account'], captured_at=time.time(),
                         sequence=wire.one(root, 2, 0), context=base64.b64encode(context).decode(),
                         identity_cookie=value['identity_cookie'], headers=headers))


class Provider:
    transport = 'http'

    def __init__(self, *, path=None, memory=None):
        self.path = Path(path) if path else vault_path()
        self.memory = validate(memory) if memory is not None else None

    def current(self):
        return validate(self.memory) if self.memory is not None else load(self.path)

    def mark_verified(self, verified):
        def update():
            data = self.current()
            data['im_verified'] = verified is True
            if self.memory is None:
                save(data, self.path)
        if self.memory is not None:
            update()
        else:
            with runtime.data_lock(self.path.parent):
                update()

    def prepare(self, operation, business_body, metadata):
        commands = {'create': 609, 'send': 100, 'im_check': 1001,
                    'conversations': 2006, 'stranger_conversations': 1001,
                    'messages': 301, 'stranger_messages': 1002}
        if operation != 'identity' and operation not in commands:
            raise ValueError('不支持的 HTTP 操作')
        if not isinstance(business_body, bytes):
            raise ValueError('请求体类型无效')
        def build():
            data = self.current()
            if metadata.get('sender_uid') != data['sender_uid']:
                raise ValueError('会话发送方与配置不一致')
            if operation == 'identity':
                if business_body:
                    raise ValueError('资料核对不能携带业务体')
                return dict(payload=b'', query={'aid': '6383'}, headers={
                    'Cookie': data['identity_cookie'], 'User-Agent': data['headers']['user-agent'],
                    'Referer': 'https://www.douyin.com/'})
            if operation != 'im_check' and data['im_verified'] is not True:
                raise ValueError('最近的 IM 身份核对未通过；先完成会话验证')
            command = commands[operation]
            if metadata.get('command') != command:
                raise ValueError('IM 命令不一致')
            expected = 1000 if operation in ('im_check', 'stranger_conversations') else 1001 if operation == 'stranger_messages' else command
            if set(wire.decode(business_body)) != {expected}:
                raise ValueError('IM 业务体与命令不一致')
            data['sequence'] += 1
            # Also migrates existing encrypted contexts on their next IM request.
            # Identity Cookie remains separate and bound to its own URL scope.
            data['headers']['cookie'] = session_cookie(data['headers']['cookie'])
            context = wire.decode(base64.b64decode(data['context']))
            # The captured stranger-list request uses inbox 1. Current web
            # one-to-one create/send calls use the conversation's inbox 0.
            # Inbox is request routing, not reusable authentication material.
            if operation in ('create', 'send', 'conversations', 'messages'):
                context[6] = [(0, 0)]
            elif operation in ('stranger_conversations', 'stranger_messages'):
                context[6] = [(0, 1)]
            encoded_context = b''.join(wire.field(n, value) for n, fields in context.items()
                                       for kind, value in fields)
            payload = (wire.field(1, command) + wire.field(2, data['sequence'])
                       + encoded_context + wire.field(8, business_body))
            if self.memory is None:
                save(data, self.path)
            return dict(payload=payload, headers=dict(data['headers']), query={})
        if self.memory is not None:
            return build()
        with runtime.data_lock(self.path.parent):
            return build()

    def ticket(self, conversation, sender_uid, recipient_uid):
        if not wire.conversation_matches(conversation.get('conversation_id', ''), sender_uid, recipient_uid):
            raise ValueError('会话双方不能核对')
        return header_value(conversation.get('_ticket'), 4096)


def check_im(provider, sender, *, exchange=None):
    result = dict(status='im_check_failed', can_send=False, live_verified=False, http_attempts=0)
    try:
        # Invalidate before the attempt: timeout/crash/rejection cannot leave
        # an earlier successful probe authorizing subsequent create/send calls.
        provider.mark_verified(False)
        body = wire.field(1000, wire.field(1, 0) + wire.field(2, 1) + wire.field(3, 1))
        prepared = provider.prepare('im_check', body, dict(sender_uid=sender, command=1001))
        sequence = wire.one(wire.decode(prepared['payload']), 2, 0)
        result['http_attempts'] = 1
        status, mime, raw = (exchange or uid_transport.request)('im_check', prepared)
        result.update(http_status=status, response_bytes=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
        if status != 200 or 'protobuf' not in mime.lower():
            return result
        response = wire.decode(raw)
        result.update(response_command=wire.one(response, 1, 0),
                      platform_code=str(wire.one(response, 3, 0, 0)),
                      sequence_matches=wire.one(response, 2, 0) == sequence,
                      sender_matches=wire.one(response, 13, 0) == int(sender),
                      platform_message_ok=wire.text(response, 4) == 'OK')
        if (wire.one(response, 1, 0) != 1001 or wire.one(response, 2, 0) != sequence
                or wire.one(response, 13, 0) != int(sender) or wire.one(response, 3, 0, 0) != 0
                or wire.text(response, 4) != 'OK'):
            return result
        content = wire.decode(wire.one(response, 6, 2, b''))
        info = wire.decode(wire.one(content, 1000, 2, b''))
        count = len(info.get(4, []))
        if set(content) == {1000} and count <= 1:
            provider.mark_verified(True)
            result.update(status='im_read_verified', requested_conversations=1, returned_conversations=count,
                          sender_uid=sender, platform_code='0', pagination=False)
    except uid_transport.TransportError as exc:
        result.update(exc.evidence)
    except Exception:
        pass
    return result


def save_status(result, *, checked_at=None):
    """Nonsecret UI diagnostic; never used as permission to send."""
    keys = {'status', 'sender_uid', 'identity_verified', 'http_status', 'platform_code',
            'transport_phase', 'transport_error', 'response_bytes', 'credential_protection'}
    safe = {key: result[key] for key in keys if key in result}
    safe.update(checked_at=time.time() if checked_at is None else checked_at, im_read_verified=result.get('status') in ('session_ready', 'im_read_verified'),
                can_send=False, live_verified=False)
    path = vault_path().with_name('status.json')
    with runtime.data_lock(path.parent):
        temporary = path.with_name('.status-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(safe, ensure_ascii=False), encoding='utf-8')
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def local_status(directory=None):
    try:
        path = Path(directory) / 'private' / 'uid-http' / 'status.json' if directory else vault_path().with_name('status.json')
        with path.open('rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError()
        value = json.loads(raw)
        return {'im_read_verified': value.get('im_read_verified') is True,
                'platform_code': str(value.get('platform_code', ''))[:16],
                'checked_at': value.get('checked_at')}
    except (OSError, ValueError, TypeError, AttributeError):
        return {'im_read_verified': False, 'platform_code': '', 'checked_at': None}


def capture_shape(value):
    """Expose only schema metadata from our read-only bootstrap, never values."""
    try:
        raw = base64.b64decode(value['payload'], validate=True)
        if len(raw) > 16384:
            return {'capture_shape': 'oversized'}
        root = wire.decode(raw)
        expected = CONTEXT_FIELDS | {1, 2, 8}
        mode = wire.one(root, 18, 0)
        result = {
            'capture_shape': 'decoded',
            'missing_fields': sorted(expected - set(root)),
            'unexpected_fields': sorted(set(root) - expected),
            'duplicate_fields': sorted(n for n, items in root.items() if n != 15 and len(items) != 1),
            'auth_mode': mode if mode in (0, 1, 2, 3, 4) else 'unrecognized',
            'token_present': bool(wire.text(root, 4)),
        }
        try:
            session_cookie(value['headers'].get('cookie'))
            result['cookie_pair_valid'] = True
        except Exception:
            result['cookie_pair_valid'] = False
        return result
    except Exception:
        return {'capture_shape': 'unrecognized'}


def bootstrap(value):
    result = dict(status='session_not_saved', can_send=False, live_verified=False, credential_file_created=False,
                  bootstrap_phase='input_validation', identity_verified=False, http_attempts=0)
    try:
        if not isinstance(value, dict) or set(value) != {'expected_account', 'identity_cookie', 'headers', 'payload'}:
            return result
        result['bootstrap_phase'] = 'identity_check'
        identity = uid_bootstrap.probe(dict(expected_account=value['expected_account'], cookie=value['identity_cookie'],
                                           user_agent=value['headers'].get('user-agent', '')))
        if identity['status'] != 'identity_verified':
            return dict(identity, bootstrap_phase='identity_check')
        result.update(identity_verified=True, http_attempts=identity['http_attempts'], bootstrap_phase='read_context_validation')
        data = from_capture(value, identity)
        result['bootstrap_phase'] = 'im_check'
        checked = check_im(Provider(memory=data), identity['sender_uid'])
        checked['http_attempts'] += identity['http_attempts']
        if checked['status'] != 'im_read_verified':
            return dict(checked, bootstrap_phase='im_check', identity_verified=True)
        result.update(http_attempts=checked['http_attempts'], bootstrap_phase='credential_save')
        path = vault_path()
        with runtime.data_lock(path.parent):
            save(data, path)
        result.update(credential_file_created=True, credential_protection='windows_user_dpapi', bootstrap_phase='config_save')
        config_path = runtime.data_dir() / 'uid-http.json'
        config_created = False
        try:
            with config_path.open('x', encoding='utf-8') as stream:
                json.dump(dict(enabled=False, sender_uid=data['sender_uid'], allowed_recipient_uids=[],
                               provider_file=os.path.relpath(Path(__file__).resolve(), runtime.data_dir())),
                          stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            config_created = True
        except FileExistsError:
            pass
        except OSError:
            result['config_write_failed'] = True
        result.update(checked, status='session_ready', identity_verified=True, im_read_verified=True,
                      bootstrap_phase='complete',
                      credential_file_created=True, credential_protection='windows_user_dpapi',
                      config_created=config_created, config_enabled=False if config_created else None,
                      source='current_account_read_authentication_context', browser_required_for_normal_http=False)
    except Exception:
        if result['bootstrap_phase'] == 'read_context_validation':
            result.update(capture_shape(value))
    return result


def main():
    result = dict(status='session_unavailable', can_send=False, live_verified=False)
    try:
        if sys.argv[1:] == ['bootstrap']:
            raw = sys.stdin.buffer.read(131073)
            result = bootstrap(json.loads(raw)) if len(raw) <= 131072 else result
        elif sys.argv[1:] == ['check']:
            provider = Provider()
            sender = provider.current()['sender_uid']
            identity = uid_transport.verify_identity({'sender_uid': sender}, provider=provider)
            result = check_im(provider, sender) if identity['status'] == 'identity_verified' else identity
            if identity['status'] == 'identity_verified':
                result['http_attempts'] += 1
            result.update(browser_started=False, credential_source='windows_user_dpapi', identity_verified=identity['status'] == 'identity_verified')
    except Exception:
        pass
    if sys.argv[1:] in (['bootstrap'], ['check']):
        try:
            save_status(result)
        except Exception:
            result['status_record_saved'] = False
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('session_ready', 'im_read_verified') else 2


if __name__ == '__main__':
    raise SystemExit(main())
