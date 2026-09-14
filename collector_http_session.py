"""Explicit browser bootstrap; normal collection only reads a DPAPI snapshot.

Each endpoint has its own URL-scoped cookie set. No credential export, automatic
browser launch, session renewal, IM mutation, or plaintext fallback.
"""
import json
import os
import hashlib
import math
from pathlib import Path
import sys
import time
import uuid

import runtime
import uid_bootstrap
import uid_session

ORIGIN = 'https://www.douyin.com'
PATHS = {
    'identity': '/aweme/v1/web/user/profile/self/',
    'comments': '/aweme/v1/web/comment/list/',
    'replies': '/aweme/v1/web/comment/list/reply/',
    'search': '/aweme/v1/web/search/item/',
}
# Keep the persisted v1 snapshot schema unchanged. New read endpoints can only
# reuse cookies whose original domain and path also match their fixed URL.
READ_PATHS = {**PATHS,
    'detail': '/aweme/v1/web/aweme/detail/',
    'author': '/aweme/v1/web/aweme/post/',
    'related': '/aweme/v1/web/aweme/related/',
}
READ_OPERATIONS = tuple(key for key in READ_PATHS if key != 'identity')
MAGIC = b'CLUBOPS-COLLECTION-DPAPI-1\n'
MAX_AGE = 12 * 3600


def path(directory=None):
    return (Path(directory) if directory is not None else Path(os.environ['CLUBOPS_COLLECTION_ACCOUNT_DATA_DIR']) if os.environ.get('CLUBOPS_COLLECTION_ACCOUNT_DATA_DIR') else runtime.data_dir()) / 'private' / 'collection-http' / 'session.dpapi'


def validate(value, *, check_age=True):
    if not isinstance(value, dict) or value.get('format') != 'clubops-collection-session-1':
        raise ValueError('采集会话结构无效')
    uid_bootstrap.account(value.get('account'))
    captured=value.get('captured_at');verified=value.get('last_verified_at',captured)
    if (any(type(t) not in (int,float) or not math.isfinite(t) or t<=0 or t>time.time()+60 for t in (captured,verified))
            or verified<captured):
        raise ValueError('采集会话核验时间无效')
    if check_age and time.time()-verified>MAX_AGE:
        raise ValueError('采集会话已超过本地复用期限，请核验原账号会话')
    uid_session.header_value(value.get('user_agent'), 512)
    if not isinstance(value.get('cookies'), dict) or set(value['cookies']) != set(PATHS):
        raise ValueError('采集会话缺少按接口作用域保存的 Cookie')
    for operation, cookies in value['cookies'].items():
        if not isinstance(cookies, list) or len(cookies) > 150:
            raise ValueError('采集会话 Cookie 格式无效')
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise ValueError('Cookie 无效')
            for name in ('name', 'domain', 'path'):
                uid_session.header_value(cookie.get(name), 8192)
            if not isinstance(cookie.get('value'), str) or len(cookie['value']) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in cookie['value']):
                raise ValueError('Cookie 值无效')
            if any(c in cookie['name'] for c in ';= ') or ';' in cookie['value']:
                raise ValueError('Cookie 无效')
            if cookie['domain'].lstrip('.') not in {'douyin.com', 'www.douyin.com'}:
                raise ValueError('Cookie 域不匹配')
            prefix = cookie['path']
            target = PATHS[operation]
            if not prefix.startswith('/') or not (target == prefix or target.startswith(prefix if prefix.endswith('/') else prefix + '/')):
                raise ValueError('Cookie 路径不匹配')
            if type(cookie.get('expires')) not in (int, float) or not math.isfinite(cookie['expires']):
                raise ValueError('Cookie 到期字段无效')
    context = value.get('context')
    if not isinstance(context, dict) or context.get('platform') not in ('Win32', 'MacIntel', 'Linux x86_64'):
        raise ValueError('采集客户端上下文无效')
    for key in ('width', 'height', 'cores', 'memory'):
        if type(context.get(key)) is not int or not 1 <= context[key] <= 20000:
            raise ValueError('采集客户端上下文无效')
    return value


def cookie_header(value, operation, *, check_age=True):
    validate(value,check_age=check_age)
    if operation not in READ_PATHS:
        raise ValueError('接口无效')
    if operation in PATHS:
        selected = value['cookies'][operation]
    else:
        selected_by_scope = {}
        target = READ_PATHS[operation]
        for group in value['cookies'].values():
            for cookie in group:
                prefix = cookie['path']
                if not (target == prefix or target.startswith(prefix if prefix.endswith('/') else prefix + '/')):
                    continue
                if cookie['expires'] != -1 and cookie['expires'] <= time.time():
                    continue
                key = (cookie['name'], cookie['domain'], prefix)
                if key in selected_by_scope and selected_by_scope[key] != cookie:
                    raise ValueError('采集会话 Cookie 作用域存在冲突，请重新准备会话')
                selected_by_scope[key] = cookie
        selected = list(selected_by_scope.values())
    cookies = sorted(selected, key=lambda c: -len(c['path']))
    result = '; '.join(c['name'] + '=' + c['value'] for c in cookies if c['expires'] == -1 or c['expires'] > time.time())
    uid_session.header_value(result)
    return result


def load(directory=None, *, check_age=True):
    file = path(directory)
    if file.is_symlink():
        raise ValueError('采集会话文件不能是符号链接')
    with file.open('rb') as stream:
        raw = stream.read(131073)
    if len(raw) > 131072 or not raw.startswith(MAGIC):
        raise ValueError('采集会话文件无效')
    return validate(json.loads(uid_session.crypt(raw[len(MAGIC):], decrypt=True)),check_age=check_age)


def status(directory=None):
    try:
        value = load(directory)
        identity = identity_state(value, directory)
        return {'ready': identity['status'] != 'identity_failed', 'status': identity['status'],
                'account': value['account'], 'expires_at': value.get('last_verified_at',value['captured_at']) + MAX_AGE,
                'endpoints': {operation: endpoint_status(value, operation, directory) for operation in READ_OPERATIONS},
                'browser_used_for_read': False}
    except FileNotFoundError:
        return {'ready': False, 'status': 'needs_login', 'browser_used_for_read': False}
    except Exception:
        return {'ready': False, 'status': 'session_expired', 'browser_used_for_read': False}


def identity_state(value, directory=None):
    tag = hashlib.sha256((value['account'] + ':' + str(value['captured_at'])).encode()).hexdigest()
    file = path(directory).with_name('identity-status.json')
    try:
        if file.is_symlink() or file.stat().st_size > 1024:
            return {'status': 'identity_failed'}
        state = json.loads(file.read_text(encoding='utf-8'))
        if state.get('session_tag') == tag and state.get('status') in ('identity_verified', 'identity_failed'):
            return {'status': state['status']}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        return {'status': 'identity_failed'}
    return {'status': 'prepared'}


def replace_status(temporary, file):
    """Windows readers may briefly deny rename; never replay an HTTP request."""
    for attempt in range(4):
        try:
            temporary.replace(file)
            return
        except PermissionError:
            if attempt == 3:raise
            time.sleep((0.02,0.05,0.1)[attempt])


def record_identity_status(value, status, directory=None):
    if status not in ('identity_verified', 'identity_failed'):
        raise ValueError('身份核对状态无效')
    file = path(directory).with_name('identity-status.json')
    file.parent.mkdir(parents=True, exist_ok=True)
    if file.is_symlink():
        raise ValueError('身份状态路径无效')
    temporary = file.with_name('.identity-' + uuid.uuid4().hex + '.tmp')
    tag = hashlib.sha256((value['account'] + ':' + str(value['captured_at'])).encode()).hexdigest()
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump({'session_tag': tag, 'status': status, 'checked_at': time.time()}, stream)
        replace_status(temporary, file)
    finally:
        if temporary.exists():
            temporary.unlink()


def endpoint_state(value, operation, directory=None):
    if operation not in READ_OPERATIONS:
        raise ValueError('接口无效')
    tag = hashlib.sha256((value['account'] + ':' + str(value['captured_at'])).encode()).hexdigest()
    try:
        file = path(directory).with_name(operation + '-status.json')
        if file.is_symlink() or file.stat().st_size > 1024:
            return {'status': 'unknown'}
        state = json.loads(file.read_text(encoding='utf-8'))
        if isinstance(state, dict) and state.get('session_tag') == tag and state.get('status') in ('valid_page', 'needs_verification'):
            return {'status': state['status'], 'verification_scope': 'search' if operation == 'search' and state.get('verification_scope') == 'search' else 'account'}
    except (OSError, ValueError, KeyError):
        pass
    return {'status': 'unknown'}


def endpoint_status(value, operation, directory=None):
    return endpoint_state(value, operation, directory)['status']


def record_endpoint_status(value, operation, status, *, verification_scope='account'):
    if operation not in READ_OPERATIONS or status not in ('valid_page','needs_verification'):
        return
    file = path().with_name(operation + '-status.json')
    file.parent.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha256((value['account'] + ':' + str(value['captured_at'])).encode()).hexdigest()
    temporary = file.with_name('.status-' + uuid.uuid4().hex + '.tmp')
    if file.is_symlink():
        raise ValueError('接口状态路径无效')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            scope = 'search' if operation == 'search' and status == 'needs_verification' and verification_scope == 'search' else 'account'
            json.dump({'session_tag':tag, 'status':status, 'checked_at':time.time(), 'verification_scope':scope}, stream)
        replace_status(temporary, file)
    finally:
        if temporary.exists():
            temporary.unlink()


def bootstrap(value, *, probe=None):
    result = {'status': 'invalid_input', 'can_send': False, 'credential_file_created': False, 'bootstrap_phase': 'validation'}
    try:
        validate(value)
        result['bootstrap_phase'] = 'cookie_header'
        cookie = cookie_header(value, 'identity')
        result['bootstrap_phase'] = 'identity_http'
        identity = (probe or uid_bootstrap.probe)({'expected_account': value['account'],
            'cookie': cookie, 'user_agent': value['user_agent']})
        result['identity_status'] = identity['status']
        result['identity_evidence'] = {k: identity[k] for k in ('http_status','response_bytes','response_sha256',
            'business_code','user_present','verification_indicated','transport_error','transport_phase') if k in identity}
        if identity['status'] != 'identity_verified':
            return {**result, 'status': 'identity_failed'}
        result['bootstrap_phase'] = 'credential_save'
        value = {**value, 'sender_uid': identity['sender_uid']}
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # uid_session.save is an atomic DPAPI writer; this format has its own magic.
        with runtime.data_lock(target.parent):
            encrypted = MAGIC + uid_session.crypt(json.dumps(value, separators=(',', ':')).encode())
            temporary = target.with_suffix('.tmp')
            if target.is_symlink() or temporary.exists():
                raise ValueError('采集会话路径不可写')
            try:
                with temporary.open('xb') as stream:
                    stream.write(encrypted)
                    stream.flush()
                    import os
                    os.fsync(stream.fileno())
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return {**result, 'status': 'session_ready', 'credential_file_created': True,
                'account': value['account'], 'collection_verified': False, 'bootstrap_phase':'complete'}
    except Exception as exc:
        reasons = {'采集会话结构无效':'invalid_structure',
                   '会话元数据无效':'invalid_metadata', 'invalid account':'invalid_account',
                   '采集会话已超过本地复用期限，请手动准备会话':'local_expiry',
                   '采集会话已超过本地复用期限，请核验原账号会话':'local_expiry',
                   '采集会话缺少按接口作用域保存的 Cookie':'missing_cookie_scopes',
                   '采集会话 Cookie 格式无效':'invalid_cookie_list', 'Cookie 无效':'invalid_cookie',
                   'Cookie 值无效':'invalid_cookie_value', 'Cookie 域不匹配':'cookie_domain_mismatch',
                   'Cookie 路径不匹配':'cookie_path_mismatch', 'Cookie 到期字段无效':'invalid_cookie_expiry',
                   '采集客户端上下文无效':'invalid_client_context', '采集会话路径不可写':'credential_path_unwritable'}
        result['bootstrap_error'] = reasons.get(str(exc), 'preparation_failed')
        return result


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(110001)
        value = json.loads(raw) if len(raw) <= 110000 else None
    except Exception:
        value = None
    print(json.dumps(bootstrap(value), ensure_ascii=False))
