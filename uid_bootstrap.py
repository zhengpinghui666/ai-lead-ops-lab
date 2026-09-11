"""One read-only HTTPS identity lookup. Credentials arrive over a local stdin pipe.

No provider loading, conversation creation, messages, credential files or retries.
This bootstrap does not provide IM authentication, signatures or tickets.
"""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone

import uid_protocol
import uid_transport

DETAILS = {
    'identity_verified': 'HTTP 资料响应与指定抖音号一致，已取得数字 UID；尚未验证 IM 鉴权、签名、票据或发送。',
    'invalid_input': '账号或本机会话输入无效，没有请求平台。',
    'needs_login': '专用会话没有可用 Cookie；请在项目专用浏览器中完成登录。',
    'http_failed': '账号资料 HTTP 请求未完成，不能确认登录身份。',
    'http_rejected': '账号资料端点拒绝、限制或返回非成功状态，已停止。',
    'unrecognized_response': '未取得可识别的账号资料响应；可能需要登录、验证或当前请求契约，不能据此确定原因。',
    'account_mismatch': '资料响应不能与指定抖音号对应；不返回另一账号的身份。',
}


def account(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{2,64}', value):
        raise ValueError('invalid account')
    return value


def probe(value, *, exchange=None):
    result = dict(status='invalid_input', phase='identity', transport='http',
                  http_attempts=0, can_send=False, live_verified=False,
                  checked_at=datetime.now(timezone.utc).isoformat())
    try:
        if not isinstance(value, dict) or set(value) != {'expected_account', 'cookie', 'user_agent'}:
            raise ValueError()
        expected = account(value['expected_account'])
        cookie, agent = value['cookie'], value['user_agent']
        if any(not isinstance(v, str) or len(v) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in v)
               for v, maximum in ((cookie, 16384), (agent, 512))):
            raise ValueError()
        if not cookie.strip():
            result['status'] = 'needs_login'
        else:
            prepared = {'payload': b'', 'query': {'aid': '6383'},
                        'headers': {'Cookie': cookie, 'User-Agent': agent or 'ClubOps-Identity-Check',
                                    'Referer': 'https://www.douyin.com/'}}
            result.update(status='http_failed', http_attempts=1)
            status, mime, raw = (exchange or uid_transport.request)('identity', prepared)
            result.update(http_status=status, response_bytes=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
            if status != 200:
                result['status'] = 'http_rejected'
            elif 'json' not in mime.lower():
                result['status'] = 'unrecognized_response'
            else:
                result['status'] = 'unrecognized_response'
                # Python's JSON parser keeps arbitrary-precision integers intact.
                data = json.loads(raw)
                if isinstance(data, dict):
                    if type(data.get('status_code')) is int:
                        result['business_code'] = data['status_code']
                    result['user_present'] = isinstance(data.get('user'), dict)
                    result['verification_indicated'] = bool(data.get('verify_type') or data.get('verify_data'))
                if isinstance(data, dict) and type(data.get('status_code')) is int and data['status_code'] == 0:
                    user = data.get('user')
                    if isinstance(user, dict):
                        visible = user.get('unique_id') or user.get('short_id')
                        if type(visible) in (str, int) and str(visible) == expected:
                            raw_uid = user.get('uid')
                            if type(raw_uid) in (str, int):
                                sender = uid_protocol.numeric_uid(str(raw_uid))
                                result.update(status='identity_verified', sender_uid=sender, expected_account=expected)
                        else:
                            result['status'] = 'account_mismatch'
    except uid_transport.TransportError as exc:
        result.update(exc.evidence)
    except Exception:
        # Never return response bodies, cookies, headers or exception text.
        pass
    result['detail'] = DETAILS[result['status']]
    return result


def main():
    try:
        raw = sys.stdin.buffer.read(32769)
        value = json.loads(raw) if len(raw) <= 32768 else None
    except Exception:
        value = None
    result = probe(value)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'identity_verified' else 2


if __name__ == '__main__':
    raise SystemExit(main())
