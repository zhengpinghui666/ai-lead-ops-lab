"""One explicitly scoped HTTP IM test; not a bulk or unattended sender.

The OpenAPI account/permission and event context must already exist. This module
does not log in, launch a browser, derive OpenIDs from Douyin numbers, or bypass
platform checks. A configured adapter is NOT a verified live connection.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

SENDER = '1267597446'
RECIPIENT = '34575459517'
TEXT = '【ClubOps 测试】这是一条单次私信收发测试消息，无需提供个人信息。测试编号：CO-DM-01。'
HOST = 'open.douyin.com'
PATH = '/im/send/msg/'
CONTEXT_FILE = 'messaging-http-context.json'
LEDGER_FILE = 'messaging-http-test.db'
TOKEN_ENV = 'CLUBOPS_DM_ACCESS_TOKEN'
MAX_BYTES = 65536
SCENES = {'im_reply_msg', 'im_enter_direct_msg'}
FIELDS = {'sender_douyin_id', 'recipient_douyin_id', 'sender_open_id',
          'recipient_open_id', 'sender_authorized', 'identity_verification_note',
          'scopes', 'scene', 'event_time', 'msg_id', 'conversation_id'}
_ACTIVE = set()
_GUARD = threading.Lock()


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_context(data_dir):
    path = Path(data_dir) / CONTEXT_FILE
    if not path.exists():
        return {}
    try:
        with path.open('rb') as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError()
        context = json.loads(raw)
        if not isinstance(context, dict) or set(context) - FIELDS:
            raise ValueError()
        return context
    except (OSError, ValueError, UnicodeError):
        raise ValueError('HTTP 私信配置无效；请检查配置结构，不要在配置内填写令牌或 Cookie') from None


def valid_string(value, limit=1024):
    return isinstance(value, str) and 0 < len(value) <= limit and value == value.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def prepare(data_dir, environ=None):
    """Local validation only. No network and no claim of verified permissions."""
    env = os.environ if environ is None else environ
    issues = []
    try:
        context = read_context(data_dir)
    except ValueError as error:
        return {}, '', [str(error)]
    token = env.get(TOKEN_ENV, '')
    if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in token):
        issues.append('缺少有效的发送账号 OpenAPI 令牌配置')
    if context.get('sender_douyin_id') != SENDER or context.get('recipient_douyin_id') != RECIPIENT:
        issues.append('尚未配置本次指定的发送方与接收方')
    for key, label in [('sender_open_id', '发送方 OpenID'), ('recipient_open_id', '接收方 OpenID'),
                       ('msg_id', '事件消息 ID'), ('conversation_id', '会话 ID')]:
        if not valid_string(context.get(key)):
            issues.append('缺少有效的' + label)
    sender, recipient = context.get('sender_open_id'), context.get('recipient_open_id')
    if sender and recipient and (sender == recipient or sender in (SENDER, RECIPIENT) or recipient in (SENDER, RECIPIENT)):
        issues.append('不能将抖音号直接当作 OpenID，也不能向发送账号自身投递')
    if context.get('sender_authorized') is not True or not valid_string(context.get('identity_verification_note'), 1000):
        issues.append('尚未核对账号与 OpenID 的对应关系及经营者授权')
    scopes = context.get('scopes')
    if not isinstance(scopes, list) or 'im.direct_message' not in scopes or any(not isinstance(x, str) for x in scopes):
        issues.append('尚未登记 im.direct_message 私信权限')
    if not isinstance(context.get('scene'), str) or context['scene'] not in SCENES:
        issues.append('本适配器仅支持已有私信或进私事件的单条测试')
    try:
        event_time = datetime.fromisoformat(context.get('event_time', '').replace('Z', '+00:00'))
        if event_time.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - event_time).total_seconds() < 86400:
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        issues.append('缺少 24 小时内的真实会话事件时间')
    return context, token if not issues else '', issues


def _ledger_path(data_dir):
    return Path(data_dir).resolve() / LEDGER_FILE


def attempt(data_dir):
    path = _ledger_path(data_dir)
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute('SELECT * FROM dm_test_attempt WHERE id=1').fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        raise ValueError('测试账本无法读取；为防止重复发送，已禁止再次投递') from None


def state(data_dir, mode='live', environ=None):
    if mode != 'live':
        return None
    _, _, issues = prepare(data_dir, environ)
    try:
        record = attempt(data_dir)
    except ValueError as error:
        issues.append(str(error))
        return dict(sender=SENDER, recipient=RECIPIENT, transport='http', text=TEXT,
                    can_send=False, status='storage_error', issues=issues, attempt=None)
    status = 'not_configured' if issues else 'configured_unverified'
    if record:
        status = record['status']
        if status == 'attempting':
            with _GUARD:
                status = 'sending' if str(_ledger_path(data_dir)) in _ACTIVE else 'unknown'
    return dict(sender=SENDER, recipient=RECIPIENT, transport='http', text=TEXT,
                can_send=not issues and record is None, status=status, issues=issues, attempt=record)


def _code(value):
    if type(value) is int and 0 <= value < 10**10:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 10:
        return int(value)
    return None


def interpret(status, content_type, raw):
    result = dict(status='unknown', detail='发送结果不确定，请在收件账号核对；不会自动重发',
                  http_status=status, platform_code=None, response_sha256=hashlib.sha256(raw).hexdigest())
    if status in (401, 403, 429):
        result.update(status='rejected', detail={401: '平台要求重新授权；未确认发送', 403: '平台拒绝访问；未确认发送', 429: '平台限制请求频率；已停止，不重试'}[status])
        return result
    if 300 <= status < 400:
        result['detail'] = '接口返回跳转，未跟随跳转，也未重发；请核对结果'
        return result
    mime = content_type.split(';')[0].strip().lower()
    if status != 200 or not (mime == 'application/json' or mime.startswith('application/') and mime.endswith('+json')) or not raw:
        return result
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        return result
    if not isinstance(body, dict):
        return result
    if 'err_no' not in body and not (isinstance(body.get('data'), dict) and 'error_code' in body['data']):
        return result
    # Accept only explicit documented-style result codes, never HTTP 200 alone.
    code_values = []
    if 'err_no' in body:
        code_values.append(body['err_no'])
    for key in ('data', 'extra'):
        part = body.get(key)
        if isinstance(part, dict) and 'error_code' in part:
            code_values.append(part['error_code'])
    codes = [_code(value) for value in code_values]
    if not codes or None in codes:
        return result
    rejected = next((value for value in codes if value != 0), None)
    if rejected is not None:
        result.update(status='rejected', platform_code=rejected, detail='平台返回业务错误；已停止，不自动重试')
    else:
        result.update(status='api_accepted', platform_code=0, detail='接口返回成功；尚未确认收件方收到，不代表已送达或已读')
    return result


def http_post(context, token):
    """One HTTPS POST, fixed origin/path, no redirects, proxies or retries."""
    payload = dict(to_user_id=context['recipient_open_id'], msg_id=context['msg_id'],
                   conversation_id=context['conversation_id'], scene=context['scene'],
                   channel=3, content={'msg_type': 1, 'text': {'text': TEXT}})
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    deadline = time.monotonic() + 15

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return value

    connection = http.client.HTTPSConnection(HOST, timeout=remaining())
    try:
        connection.connect()
        connection.sock.settimeout(remaining())
        connection.request('POST', PATH + '?' + urlencode({'open_id': context['sender_open_id']}),
                           body=body, headers={'Content-Type': 'application/json',
                           'access-token': token, 'User-Agent': 'ClubOps-Single-IM-Test/1.0'})
        connection.sock.settimeout(remaining())
        response = connection.getresponse()
        if response.status != 200:
            return interpret(response.status, '', b'')
        parts, size = [], 0
        while True:
            if connection.sock:
                connection.sock.settimeout(remaining())
            else:
                remaining()
            part = response.read1(min(4096, MAX_BYTES + 1 - size))
            if not part:
                break
            size += len(part)
            if size > MAX_BYTES:
                return dict(status='unknown', detail='接口响应超过本次读取上限；结果待核对，不重发', http_status=200, platform_code=None, response_sha256='')
            parts.append(part)
        return interpret(response.status, response.getheader('Content-Type', ''), b''.join(parts))
    finally:
        connection.close()


def send_one(data_dir, mode='live', body=None, *, environ=None, transport=None):
    if mode != 'live':
        raise ValueError('HTTP 测试不能在演示工作区执行')
    if body not in (None, {}):
        raise ValueError('本次仅允许固定账号和固定文本，不接受批量或覆盖参数')
    context, token, issues = prepare(data_dir, environ)
    try:
        existing = attempt(data_dir)
    except ValueError:
        return state(data_dir, mode, environ)
    if issues or existing:
        return state(data_dir, mode, environ)
    path = _ledger_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as connection, connection:
        connection.execute('CREATE TABLE IF NOT EXISTS dm_test_attempt (id INTEGER PRIMARY KEY CHECK(id=1), sender TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, http_status INTEGER, platform_code INTEGER, response_sha256 TEXT NOT NULL DEFAULT \'\', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
        connection.execute('BEGIN IMMEDIATE')
        inserted = connection.execute("INSERT OR IGNORE INTO dm_test_attempt(id,sender,recipient,text,status,detail,created_at,updated_at) VALUES(1,?,?,?,'attempting','已登记唯一发送尝试；如进程中断，须人工核对收件结果',?,?)", (SENDER, RECIPIENT, TEXT, stamp(), stamp())).rowcount
    if not inserted:
        return state(data_dir, mode, environ)
    key = str(path)
    with _GUARD:
        _ACTIVE.add(key)
    try:
        try:
            result = (transport or http_post)(context, token)
        except Exception:
            # Exception strings may contain credentials or request payloads.
            result = dict(status='unknown', detail='网络或处理异常；可能已提交，请核对收件账号，不会自动重发', http_status=None, platform_code=None, response_sha256='')
        try:
            with closing(sqlite3.connect(path, timeout=5)) as connection, connection:
                connection.execute('UPDATE dm_test_attempt SET status=?,detail=?,http_status=?,platform_code=?,response_sha256=?,updated_at=? WHERE id=1',
                    (result['status'], result['detail'], result['http_status'], result['platform_code'], result['response_sha256'], stamp()))
        except sqlite3.Error:
            # The committed attempt still blocks duplicates if saving the result fails.
            pass
    finally:
        with _GUARD:
            _ACTIVE.discard(key)
    return state(data_dir, mode, environ)
