"""Bounded HTTP reads, independent of browser/IM code. No automatic retries.

The optional upstream parameter implementation remains outside the source package,
with its original license and fixed-byte integrity check. Network success must be
validated separately from local parameter calculation.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading
import time
import types
from urllib.parse import urlencode, quote

import collector_http_session as sessions

BASE = Path(__file__).resolve().parent
SHA = 'df8aced70e476ae3330fa913186f3207b4843201'
DIGEST = '6a0b5b8b3666522ddb8e14ee760535455e8734e576d4707132d3ff6992e6d355'
SOURCE = BASE / '.tools' / 'tiktok-params' / SHA / 'src/encrypt/aBogus.py'
PROVIDER = 'tiktok-abogus-dynamic-' + SHA[:7]
F2_SHA = '7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3'
F2_SOURCE = BASE / '.tools/f2-params' / F2_SHA / 'f2/utils/abogus.py'
F2_DIGEST = '82cc97b63aab2ac80a5c312fe52850ccc74d6fdffe6edaee4e58add14083a3c3'
MAX_BODY = 2 * 1024 * 1024
DETAILS = {
    'empty_response': 'HTTP 返回空响应，未取得有效评论；不按零评论结算。',
    'schema_changed': '响应结构或分页状态不符合约定，已停止并保留此前结果。',
    'rate_limited': '平台限制请求频率，已停止整批，不自动重试。',
    'access_denied': '平台拒绝请求，已停止整批。',
    'needs_login': '需要手动准备采集会话，正常 HTTP 任务不会自动打开浏览器。',
    'session_expired': '本地会话过期或不可用，请手动准备采集会话。',
    'identity_failed': 'HTTP 身份核对未通过，未开始读取评论。',
    'needs_verification': '平台要求验证，已停止；请手动处理。',
    'network_error': 'HTTP 连接失败，未自动重试。',
    'timeout': '本批 HTTP 读取超过时限，已停止。',
    'resource_limited': 'HTTP 读取超过响应大小或请求预算，已停止。',
    'cancelled': '采集已停止，已入库数据保留。',
    'dependency_missing': 'HTTP 运行依赖未安装或校验失败，请运行采集依赖安装脚本。',
    'upstream_rejected': '平台业务响应未通过校验，已停止。',
}


class ReadError(ValueError):
    def __init__(self, status, evidence=None):
        self.status = status
        self.evidence = evidence or {}
        super().__init__(DETAILS.get(status, DETAILS['schema_changed']))


def numeric(value):
    if type(value) not in (str, int):
        return ''
    value = str(value)
    return value if re.fullmatch(r'\d{5,30}', value) else ''


def python_path():
    return BASE / '.tools/douyin-http-venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')


def runtime_status(data_directory=None):
    intact = all(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == digest
                 for p, digest in ((SOURCE, DIGEST), (F2_SOURCE, F2_DIGEST)))
    session = sessions.status(data_directory)
    return {'installed': python_path().is_file() and intact, 'session': session,
            'provider': PROVIDER, 'transport': 'http', 'browser_used_for_read': False,
            'live_verified': False}


class Signer:
    provider = PROVIDER
    def __init__(self, user_agent):
        if not SOURCE.is_file() or hashlib.sha256(SOURCE.read_bytes()).hexdigest() != DIGEST:
            raise ReadError('dependency_missing')
        # The audited file's only app-level import is this constant. Do not import
        # the upstream app, downloader, configuration, entry point or JS bundles.
        custom = types.ModuleType('src.custom')
        custom.USERAGENT = user_agent
        parent = types.ModuleType('src')
        parent.__path__ = []
        saved = {key: sys.modules.get(key) for key in ('src', 'src.custom')}
        try:
            sys.modules.update({'src': parent, 'src.custom': custom})
            spec = importlib.util.spec_from_file_location('_clubops_tiktok_params', SOURCE)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.implementation = module.ABogus(user_agent=user_agent)
        except Exception:
            raise ReadError('dependency_missing') from None
        finally:
            for key, value in saved.items():
                if value is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = value

    def sign(self, params):
        query = urlencode(params)
        value = self.implementation.get_value(query, method='GET')
        if not isinstance(value, str) or not 40 <= len(value) <= 2048:
            raise ReadError('dependency_missing')
        return query + '&' + urlencode({'a_bogus': value})


class F2Signer:
    provider = 'f2-abogus-get-' + F2_SHA[:7]

    def __init__(self, session):
        if not F2_SOURCE.is_file() or hashlib.sha256(F2_SOURCE.read_bytes()).hexdigest() != F2_DIGEST:
            raise ReadError('dependency_missing')
        spec = importlib.util.spec_from_file_location('_clubops_f2_params', F2_SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ctx = session['context']
        width, height = ctx['width'], ctx['height']
        fp = '|'.join(map(str, [width, height, width, height, 0, 0, 0, 0, width, height,
                               width, height, width, height, 24, 24, ctx['platform']]))
        self.implementation = module.ABogus(user_agent=session['user_agent'], fp=fp, options=[0, 1, 8])

    def sign(self, params):
        query = urlencode(params)
        value = self.implementation.generate_abogus(query)[1]
        if not isinstance(value, str) or not 40 <= len(value) <= 2048:
            raise ReadError('dependency_missing')
        return query + '&' + urlencode({'a_bogus': value})


def page_shape(body):
    """Fixed structural fields only; never copy comment text, tokens or messages."""
    result = {'version': 'http-page-shape-v1', 'body_type': type(body).__name__}
    if not isinstance(body, dict):
        return result
    for key in ('status_code', 'has_more', 'cursor', 'total', 'comments'):
        value = body.get(key)
        result[key + '_type'] = type(value).__name__ if key in body else 'missing'
        if key != 'comments' and type(value) is int and -(2**63) <= value < 2**63:
            result[key] = value
    result['verification_indicated'] = bool(body.get('verify_type') or body.get('verify_data'))
    if isinstance(body.get('comments'), list):
        result['comments_count'] = len(body['comments'])
    return result


def parse_page(body, operation, video='', parent='', title=''):
    """Strict known containers only; IDs remain arbitrary-precision strings."""
    if not isinstance(body, dict) or type(body.get('status_code')) is not int:
        raise ReadError('schema_changed', {'reason': 'invalid_status_code'})
    if body['status_code'] != 0:
        raise ReadError('upstream_rejected', {'business_code': body['status_code']})
    if body.get('verify_type') or body.get('verify_data'):
        raise ReadError('needs_verification')
    nil_info = body.get('search_nil_info')
    if isinstance(nil_info, dict):
        reason = nil_info.get('search_nil_type')
        if isinstance(reason, str) and any(word in reason.lower() for word in ('verify', 'antispam', 'risk', 'captcha')):
            raise ReadError('needs_verification', {'reason': 'search_verification_required'})
    more = body.get('has_more')
    if type(more) is not int or more not in (0, 1):
        raise ReadError('schema_changed', {'reason': 'invalid_has_more'})
    cursor = body.get('cursor', body.get('offset'))
    if cursor is None and not more:
        cursor = 0
    if type(cursor) is not int or not 0 <= cursor < 2**63:
        raise ReadError('schema_changed', {'reason': 'invalid_cursor'})
    key = 'data' if operation == 'search' else 'comments'
    items = body.get(key)
    # Replies can report a nonzero statistical total while exposing no rows on
    # the terminal page. This says nothing about deletion or the total count.
    empty_replies = (operation == 'replies' and 'comments' in body and items is None
                     and more == 0 and type(body.get('total')) is int and 0 <= body['total'] < 2**63)
    if empty_replies:
        items = []
    if (items is None and operation == 'comments' and 'comments' in body
            and type(body.get('total')) is int and body['total'] == 0 and more == 0):
        items = []
    if not isinstance(items, list):
        raise ReadError('schema_changed', {'reason': 'invalid_page_container'})
    if len(items) > 1000 or (not items and more):
        raise ReadError('schema_changed', {'reason': 'invalid_page_size' if items else 'empty_page_with_more'})
    rows, skipped, reply_targets, seen = [], 0, [], set()
    nontext = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        if operation == 'search':
            import video_discovery
            item = item.get('aweme_info', item)
            if not isinstance(item, dict) or not numeric(item.get('aweme_id')):
                skipped += 1
                continue
            vid = numeric(item['aweme_id'])
            row = video_discovery.video_row(item)
            identity = vid
        else:
            cid = numeric(item.get('cid'))
            vid = numeric(item.get('aweme_id')) if item.get('aweme_id') is not None else video
            raw = item.get('text')
            thread = item.get('reply_id')
            direct = item.get('reply_to_reply_id')
            refs = [r for r in (thread, direct) if r not in (None, '', 0, '0')]
            if (vid != video or not cid or not isinstance(raw, str)
                    or any(not numeric(r) for r in refs) or parent and numeric(thread) and numeric(thread) != parent):
                skipped += 1
                continue
            reference = numeric(direct) or numeric(thread) or parent
            if reference == cid:
                skipped += 1
                continue
            if not raw.strip():
                skipped += 1
                nontext += 1
                continue
            user = item.get('user') if isinstance(item.get('user'), dict) else {}
            created = item.get('create_time')
            row = {'comment_id': cid, 'video_id': video, 'video_title': title or video,
                   'text': raw.strip()[:5000], 'user_id': numeric(user.get('uid')),
                   'sec_uid': user.get('sec_uid') if isinstance(user.get('sec_uid'), str) else '',
                   'nickname': str(user.get('nickname') or '')[:120], 'parent_comment_id': reference,
                   'published_at': created if type(created) is int and 0 < created < 32503680000 else None}
            identity = cid
            if operation == 'comments' and type(item.get('reply_comment_total')) is int and item['reply_comment_total'] > 0:
                reply_targets.append(cid)
        if identity not in seen:
            seen.add(identity)
            rows.append(row)
    if items and not rows and nontext != len(items):
        raise ReadError('schema_changed')
    search_id = (body.get('log_pb') or {}).get('impr_id', '') if isinstance(body.get('log_pb'), dict) else ''
    if operation == 'search' and more and (not isinstance(search_id, str) or not search_id):
        raise ReadError('schema_changed')
    result = {'rows': rows, 'cursor': cursor, 'has_more': bool(more), 'search_id': search_id,
              'skipped': skipped, 'skipped_reasons': {'non_text':nontext,'invalid_record':skipped-nontext}, 'reply_targets': reply_targets}
    if empty_replies:
        result['reply_visibility'] = {'state': 'terminal_without_visible_replies', 'declared_total': body['total'], 'returned_rows': 0}
    return result


def exchange(url, headers, cancelled):
    """One GET, TLS verified, no redirects, no ambient proxies, bounded body."""
    from curl_cffi.requests import Session
    raw = bytearray()
    overflow = False
    def receive(block):
        nonlocal overflow
        if cancelled():
            return 0
        if len(raw) + len(block) > MAX_BODY:
            overflow = True
            return 0
        raw.extend(block)
        return len(block)
    try:
        with Session(trust_env=False) as client:
            response = client.get(url, headers=headers, timeout=15, allow_redirects=False,
                verify=True, impersonate='chrome', content_callback=receive)
            # A callback may stop consuming the body without curl raising.
            if cancelled():
                raise ReadError('cancelled')
            if overflow:
                raise ReadError('resource_limited')
            return response.status_code, response.headers.get('content-type', ''), bytes(raw)
    except Exception:
        if cancelled():
            raise ReadError('cancelled') from None
        raise ReadError('resource_limited' if overflow else 'network_error') from None


class Client:
    def __init__(self, session, *, signer=None, transport=None, cancelled=lambda: False, diagnostic=lambda x: None,
                 request_limit=24):
        self.session = session
        self.signer = signer
        self.signers = {}  # Endpoint choice is fixed, never an automatic retry/fallback.
        self.real_transport = transport is None
        self.transport = transport or exchange
        self.cancelled, self.diagnostic = cancelled, diagnostic
        self.count = 0
        self.request_limit = request_limit
        self.deadline = time.monotonic() + 180
        self.lock = threading.Lock()

    def check_gate(self, operation):
        if self.real_transport and sessions.identity_state(self.session)['status'] == 'identity_failed':
            self.diagnostic({'operation': 'identity', 'status': 'identity_failed', 'transport': 'http',
                             'request_number': 0, 'reason': 'identity_session_gate'})
            raise ReadError('identity_failed')
        gate = sessions.endpoint_state(self.session, operation) if self.real_transport else {}
        if self.real_transport and gate.get('status') != 'needs_verification':
            for other in sessions.READ_OPERATIONS:
                candidate = sessions.endpoint_state(self.session, other)
                if candidate.get('status') == 'needs_verification' and candidate.get('verification_scope') == 'account':
                    gate = candidate
                    break
        if gate.get('status') == 'needs_verification':
            evidence = {'operation': operation, 'status': 'needs_verification', 'transport': 'http',
                        'request_number': 0, 'reason': 'session_gate', 'verification_scope': gate['verification_scope']}
            self.diagnostic(evidence)
            raise ReadError('needs_verification')

    def page(self, operation, *, video='', parent='', keyword='', cursor=0, count=10, search_id='', title='', sec_uid=''):
        if operation not in sessions.READ_OPERATIONS or type(cursor) is not int or not 0 <= cursor < 2**63 or type(count) is not int or not 1 <= count <= 20:
            raise ValueError('读取参数无效')
        if operation not in ('search', 'author') and not numeric(video) or operation == 'replies' and not numeric(parent):
            raise ValueError('视频或父评论 ID 无效')
        if operation == 'author' and (not isinstance(sec_uid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', sec_uid)):
            raise ValueError('作者 sec_uid 无效')
        if operation in ('detail', 'related') and cursor:
            raise ValueError('该发现入口只支持单页读取')
        if operation == 'search' and (not isinstance(keyword, str) or not 1 <= len(keyword) <= 80):
            raise ValueError('搜索词无效')
        if self.cancelled():
            raise ReadError('cancelled')
        self.check_gate(operation)
        if time.monotonic() >= self.deadline:
            raise ReadError('timeout')
        cookie = sessions.cookie_header(self.session, operation)
        agent, context = self.session['user_agent'], self.session['context']
        version = re.search(r'Chrome/([\d.]+)', agent)
        if not version:
            raise ReadError('session_expired')
        params = {'device_platform': 'webapp', 'aid': '6383', 'channel': 'channel_pc_web',
            'update_version_code': '170400', 'pc_client_type': '1', 'version_code': '170400', 'version_name': '17.4.0',
            'cookie_enabled': 'true', 'screen_width': str(context['width']), 'screen_height': str(context['height']),
            'browser_language': 'zh-CN', 'browser_platform': context['platform'], 'browser_name': 'Chrome',
            'browser_version': version[1], 'browser_online': 'true', 'engine_name': 'Blink', 'engine_version': version[1],
            'os_name': 'Windows' if context['platform'] == 'Win32' else 'Mac OS' if context['platform'] == 'MacIntel' else 'Linux',
            'os_version': '10' if context['platform'] == 'Win32' else '', 'cpu_core_num': str(context['cores']),
            'device_memory': str(context['memory']), 'platform': 'PC', 'count': str(count), 'support_h265': '1', 'support_dash': '1'}
        cookie_values = dict(pair.split('=', 1) for pair in cookie.split('; '))
        # Existing session tokens only; never fabricate/refresh challenge credentials.
        for key in (('msToken', 'uifid') if operation == 'comments' else ('msToken',)):
            if cookie_values.get(key):
                params[key] = cookie_values[key]
        if operation == 'comments':
            params.update(aweme_id=video, cursor=str(cursor), item_type='0', insert_ids='', pc_img_format='webp')
        elif operation == 'replies':
            params.update(item_id=video, comment_id=parent, cursor=str(cursor), item_type='0', cut_version='1')
        elif operation == 'detail':
            params.update(aweme_id=video)
        elif operation == 'author':
            params.update(sec_user_id=sec_uid, max_cursor=str(cursor))
        elif operation == 'related':
            params.update(aweme_id=video, filterGids=video,
                awemePcRecRawData='{"is_client":"false"}', sub_channel_id='3')
        else:
            params.update(keyword=keyword, offset=str(cursor), search_channel='aweme_video_web', search_source='normal_search',
                query_correct_type='1', is_filter_search='1', enable_history='1', from_group_id='',
                need_filter_settings='1' if cursor == 0 else '0', list_type='single', sort_type='0', publish_time='0',
                filter_duration='', search_range='0', pc_search_top_1_params='')
            if search_id:
                params['search_id'] = search_id
        with self.lock:
            if self.cancelled():
                raise ReadError('cancelled')
            if self.count >= self.request_limit:
                budget = {'reason': 'request_budget', 'requests_used': self.count, 'request_limit': self.request_limit}
                self.diagnostic({'operation':operation,'transport':'http','status':'resource_limited',**budget})
                raise ReadError('resource_limited', budget)
            if self.signer is None and operation not in self.signers:
                self.signers[operation] = Signer(agent) if operation == 'comments' else F2Signer(self.session)
            signer = self.signer or self.signers[operation]
            query = signer.sign(params)
            if operation != 'comments' and cookie_values.get('s_v_web_id'):
                query += '&' + urlencode({'verifyFp': cookie_values['s_v_web_id'], 'fp': cookie_values['s_v_web_id']})
            self.count += 1
            number = self.count
        headers = {'Cookie': cookie, 'User-Agent': agent, 'Accept': 'application/json',
                   'Referer': sessions.ORIGIN + ('/video/' + video if video else '/'), 'Accept-Language': 'zh-CN,zh;q=0.9'}
        if operation == 'search':
            headers['Referer'] = sessions.ORIGIN + '/search/' + quote(keyword) + '?type=video'
        elif operation == 'author':
            headers['Referer'] = sessions.ORIGIN + '/user/' + sec_uid
        evidence = {'operation': operation, 'request_number': number, 'provider': getattr(signer, 'provider', 'injected-test-provider'), 'transport': 'http'}
        if video:
            evidence['video_id'] = str(video)
        if parent:
            evidence['parent_comment_id'] = str(parent)
        evidence['requested_cursor'] = cursor
        try:
            if self.cancelled():
                raise ReadError('cancelled')
            evidence['request_started_ms'] = int(time.monotonic() * 1000)
            status, mime, raw = self.transport(sessions.ORIGIN + sessions.READ_PATHS[operation] + '?' + query, headers, self.cancelled)
            evidence['response_received_ms'] = int(time.monotonic() * 1000)
            evidence.update(http_status=status, response_bytes=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
            if self.cancelled():
                raise ReadError('cancelled')
            if status != 200:
                raise ReadError({401: 'needs_login', 403: 'access_denied', 429: 'rate_limited'}.get(status, 'upstream_rejected'))
            if not raw:
                raise ReadError('empty_response')
            if len(raw) > MAX_BODY:
                raise ReadError('resource_limited')
            if 'json' not in mime.lower():
                raise ReadError('schema_changed')
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ReadError('schema_changed', {'reason': 'invalid_json'}) from None
            if operation in ('comments', 'replies'):
                evidence['response_shape'] = page_shape(body)
            if operation == 'search' and isinstance(body, dict):
                evidence['response_keys'] = [k[:60] for k in body if isinstance(k, str)][:40]
                nil = body.get('search_nil_info')
                if isinstance(nil, dict) and isinstance(nil.get('search_nil_type'), str):
                    reason = nil['search_nil_type']
                    if re.fullmatch(r'[a-zA-Z0-9_]{1,80}', reason):
                        evidence['search_nil_type'] = reason
            if operation in ('detail', 'author', 'related'):
                from video_discovery import parse_discovery, discovery_shape
                evidence['response_shape'] = discovery_shape(body)
                result = parse_discovery(body, operation, video=video, sec_uid=sec_uid, requested_cursor=cursor)
            else:
                result = parse_page(body, operation, video, parent, title)
            evidence.update(status='valid_page', rows=len(result['rows']), skipped=result['skipped'],
                            skipped_reasons=result['skipped_reasons'],has_more=result['has_more'],next_cursor=result['cursor'])
            if result.get('reply_visibility'):
                evidence['reply_visibility'] = result['reply_visibility']
            if result.get('page_visibility'):
                evidence['page_visibility'] = result['page_visibility']
            if self.real_transport:
                sessions.record_endpoint_status(self.session, operation, 'valid_page')
            return result
        except ReadError as exc:
            evidence.update(status=exc.status, **exc.evidence)
            if self.real_transport and exc.status == 'needs_verification':
                scope = 'search' if operation == 'search' and evidence.get('reason') == 'search_verification_required' and evidence.get('search_nil_type') == 'verify_check' else 'account'
                sessions.record_endpoint_status(self.session, operation, 'needs_verification', verification_scope=scope)
            raise
        finally:
            # Strictly constructed fields; never URLs, query strings, headers, tokens or raw bodies.
            if 'request_started_ms' in evidence:
                evidence['request_elapsed_ms'] = max(0, int(time.monotonic() * 1000) - evidence['request_started_ms'])
            self.diagnostic(evidence)
