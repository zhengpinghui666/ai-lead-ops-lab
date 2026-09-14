"""Pure HTTPS transport with an explicit local session/signing provider contract.

The provider must supply fresh credentials/signatures bound to each request.
There is intentionally no copied template, default credential or browser fallback.
"""
import hashlib
import http.client
import importlib.util
import json
import re
import ssl
import time
from pathlib import Path
from urllib.parse import urlencode

import uid_protocol as wire

ENDPOINTS = {
    'identity': ('www.douyin.com', '/aweme/v1/web/user/profile/self/', 'GET'),
    'create': ('imapi.douyin.com', '/v2/conversation/create', 'POST'),
    'send': ('imapi.douyin.com', '/v1/message/send', 'POST'),
    'im_check': ('imapi.douyin.com', '/v1/stranger/get_conversation_list', 'POST'),
    'conversations': ('imapi.douyin.com', '/v1/conversation/list', 'POST'),
    'stranger_conversations': ('imapi.douyin.com', '/v1/stranger/get_conversation_list', 'POST'),
    'messages': ('imapi.douyin.com', '/v1/message/get_by_conversation', 'POST'),
    'stranger_messages': ('imapi.douyin.com', '/v1/stranger/get_messages', 'POST'),
    'group_join': ('imapi.douyin.com', '/v1/conversation/add_participants', 'POST'),
    'group_leave': ('imapi.douyin.com', '/v1/conversation/leave', 'POST'),
    'group_members': ('imapi.douyin.com', '/v1/conversation/participants_list', 'POST'),
}

IDENTITY_REASONS = frozenset({'preparation_failed', 'response_unavailable', 'http_status_rejected',
    'unexpected_content_type', 'invalid_json', 'invalid_profile', 'platform_rejected',
    'sender_mismatch', 'identity_verified', 'transport_failed'})


class TransportError(ValueError):
    """Fixed diagnostics only: never expose a URL, header or exception string."""

    def __init__(self, reason, phase, *, http_status=None, response_bytes=None):
        reasons = {'timeout', 'tls_verification_failed', 'connection_failed',
                   'invalid_response', 'response_exceeds_bound', 'transport_failed'}
        phases = {'connect', 'request', 'response_headers', 'response_body'}
        self.evidence = {'transport_error': reason if reason in reasons else 'transport_failed',
                         'transport_phase': phase if phase in phases else 'request'}
        if type(http_status) is int and 100 <= http_status <= 599:
            self.evidence['http_status'] = http_status
        if type(response_bytes) is int and 0 <= response_bytes <= 262145:
            self.evidence['response_bytes'] = response_bytes
        super().__init__(self.evidence['transport_error'])


def load_provider(path):
    spec = importlib.util.spec_from_file_location('_clubops_uid_session', Path(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    provider = module.Provider()
    if getattr(provider, 'transport', '') != 'http' or not callable(getattr(provider, 'prepare', None)) or not callable(getattr(provider, 'ticket', None)):
        raise ValueError('本机会话提供器不满足纯 HTTP 接口约定')
    return provider


def request(operation, prepared):
    host, path, method = ENDPOINTS[operation]
    query, headers, payload = prepared.get('query', {}), prepared.get('headers', {}), prepared.get('payload', b'')
    if not isinstance(query, dict) or not isinstance(headers, dict) or not isinstance(payload, bytes):
        raise ValueError('Invalid prepared request')
    if len(payload) > 131072 or len(headers) > 30 or len(query) > 40:
        raise ValueError('Request exceeds bound')
    for mapping in (query, headers):
        for k, v in mapping.items():
            if not isinstance(k, str) or not isinstance(v, str) or len(v) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in k + v):
                raise ValueError('Invalid request metadata')
    # HTTP/2 routing pseudo-headers are not HTTP/1.1 header fields. Validate
    # before opening a connection so malformed provider output stays local.
    for name, value in headers.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError('Invalid HTTP header name')
        try:
            value.encode('latin-1')
        except UnicodeEncodeError:
            raise ValueError('Invalid HTTP header encoding') from None
    if any(k.lower() in ('host', 'content-length', 'transfer-encoding', 'connection') for k in headers):
        raise ValueError('Forbidden transport header')
    headers = {k.lower(): v for k, v in headers.items()}
    headers['accept'] = 'application/json' if operation == 'identity' else 'application/x-protobuf'
    if operation != 'identity':
        headers['content-type'] = 'application/x-protobuf'
    if operation == 'identity' and payload:
        raise ValueError('Identity request cannot contain a message')
    deadline = time.monotonic() + 15
    connection = http.client.HTTPSConnection(host, timeout=15)
    phase, status, size = 'connect', None, 0
    try:
        connection.connect()
        connection.sock.settimeout(max(0.01, deadline - time.monotonic()))
        phase = 'request'
        connection.request(method, path + ('?' + urlencode(query) if query else ''), body=payload or None, headers=headers)
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError()
        connection.sock.settimeout(left)
        phase = 'response_headers'
        response = connection.getresponse()
        status = response.status
        phase = 'response_body'
        parts, size = [], 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError()
            if connection.sock:
                connection.sock.settimeout(left)
            block = response.read1(min(8192, 262145 - size))
            if not block:
                break
            size += len(block)
            if size > 262144:
                raise TransportError('response_exceeds_bound', phase, http_status=status, response_bytes=size)
            parts.append(block)
        return response.status, response.getheader('Content-Type', ''), b''.join(parts)
    except TransportError:
        raise
    except Exception as exc:
        reason = ('tls_verification_failed' if isinstance(exc, ssl.SSLCertVerificationError)
                  else 'timeout' if isinstance(exc, TimeoutError)
                  else 'invalid_response' if isinstance(exc, http.client.HTTPException)
                  else 'connection_failed' if isinstance(exc, OSError)
                  else 'transport_failed')
        raise TransportError(reason, phase, http_status=status, response_bytes=size) from None
    finally:
        connection.close()


def validate_envelope(prepared, command, body):
    root = wire.decode(prepared.get('payload', b''))
    if wire.one(root, 1, 0) != command or wire.one(root, 8, 2) != body:
        raise ValueError('签名提供器修改了目标或消息内容，已停止')
    sequence = wire.one(root, 2, 0)
    if type(sequence) is not int or not 0 < sequence < 2**63:
        raise ValueError('缺少有效的请求关联序列号')
    return sequence


def verify_identity(config, *, provider=None, exchange=None):
    """Check only the configured sender. Never create a conversation or send."""
    result = dict(status='failed', phase='identity', transport='http',
                  live_verified=False, can_send=False, identity_reason='preparation_failed',
                  detail='登录身份核对失败；未创建会话或发送消息')
    try:
        sender = wire.numeric_uid(config['sender_uid'])
        provider = provider or load_provider(config['provider_file'])
        prepared = provider.prepare('identity', b'', dict(sender_uid=sender))
        if not isinstance(prepared, dict) or prepared.get('payload', b'') != b'':
            raise ValueError('Identity must have an empty body')
        result['identity_reason'] = 'response_unavailable'
        status, mime, raw = (exchange or request)('identity', prepared)
        result.update(http_status=status, response_bytes=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
        result['identity_reason'] = 'http_status_rejected'
        if status != 200:
            return result
        result['identity_reason'] = 'unexpected_content_type'
        if 'json' not in mime.lower():
            return result
        result['identity_reason'] = 'invalid_json'
        data = json.loads(raw)
        result['identity_reason'] = 'invalid_profile'
        if not isinstance(data, dict):
            return result
        # A UID echoed alongside a business error is not a successful login.
        if type(data.get('status_code')) is int and -(2**63) <= data['status_code'] < 2**63:
            result['platform_code'] = data['status_code']
        if 'status_code' in data and (type(data['status_code']) is not int or data['status_code'] != 0):
            result['identity_reason'] = 'platform_rejected'
            return result
        user = data.get('user')
        observed = user.get('uid') if isinstance(user, dict) else None
        if type(observed) not in (str, int):
            return result
        result['sender_matches'] = wire.numeric_uid(str(observed)) == sender
        if not result['sender_matches']:
            result['identity_reason'] = 'sender_mismatch'
            result['detail'] = '实际登录身份无法与配置 UID 对应；未创建会话或发送消息'
            return result
        result.update(status='identity_verified', identity_reason='identity_verified', sender_uid=sender,
                      detail='发送账号身份已核对；尚未验证 IM 鉴权、会话票据或发送能力。未创建会话或发送消息。')
    except TransportError as exc:
        result.update(identity_reason='transport_failed', **exc.evidence)
    except Exception:
        # Exceptions and raw profile data may include credentials or private data.
        pass
    return result


def send(config, receiver, message, client_id, before_submit, *, provider=None, exchange=None):
    """One identity GET, one conversation POST, at most one send POST."""
    stage = 'identity'
    try:
        provider = provider or load_provider(config['provider_file'])
        exchange = exchange or request
        sender = config['sender_uid']
        identity = verify_identity(config, provider=provider, exchange=exchange)
        if identity['status'] != 'identity_verified':
            return identity
        stage = 'create'
        body = wire.create_body(sender, receiver)
        prepared = provider.prepare('create', body, dict(sender_uid=sender, recipient_uid=receiver, command=609))
        sequence = validate_envelope(prepared, 609, body)
        status, mime, raw = exchange('create', prepared)
        diagnostics = dict(http_status=status, response_bytes=len(raw),
                           response_sha256=hashlib.sha256(raw).hexdigest())
        if status != 200 or 'protobuf' not in mime.lower():
            return dict(status='failed', detail='创建会话未取得有效响应；没有发送消息', phase=stage, **diagnostics)
        conversation = wire.result(raw, 609, sender, receiver, sequence=sequence)
        if conversation['status'] != 'conversation_created':
            if 'platform_code' in conversation:
                diagnostics['platform_code'] = conversation['platform_code']
            return dict(status='failed', detail='会话结果或双方身份无法核对；没有发送消息', phase=stage, **diagnostics)
        stage = 'ticket'
        ticket = provider.ticket(dict(conversation), sender, receiver)
        body = wire.send_body(conversation, client_id, message, ticket)
        stage = 'prepare_send'
        metadata = dict(sender_uid=sender, recipient_uid=receiver, client_message_id=client_id, command=100, **conversation)
        prepared = provider.prepare('send', body, metadata)
        sequence = validate_envelope(prepared, 100, body)
        before_submit()
        stage = 'send'
        status, mime, raw = exchange('send', prepared)
        if status in (401, 403, 429):
            result = dict(status='failed', detail='平台拒绝或限制发送；已停止，不重试')
        elif status != 200 or 'protobuf' not in mime.lower():
            result = dict(status='unknown', detail='提交结果不确定；请核对会话，不会重发')
        else:
            result = wire.result(raw, 100, sender, receiver, client_id, sequence=sequence)
        return dict(result, phase=stage, http_status=status, response_sha256=hashlib.sha256(raw).hexdigest(),
                    conversation_id=conversation['conversation_id'], conversation_short_id=conversation['conversation_short_id'])
    except TransportError as exc:
        return dict(status='unknown' if stage == 'send' else 'failed', phase=stage,
                    detail='HTTP 传输未完成；查看阶段记录，不会自动重发', **exc.evidence)
    except Exception:
        # Credentials can occur in exception text. Never persist exception strings.
        return dict(status='unknown' if stage == 'send' else 'failed', phase=stage,
                    detail='发送结果不确定；不会重发' if stage == 'send' else '账号、签名或会话准备失败；消息未提交')
