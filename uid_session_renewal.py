"""Bounded HTTP-only renewal for an already configured, actively used account."""
import hashlib
import json
from pathlib import Path
import threading
import time

import clubops as app
import uid_messaging
import uid_protocol
import uid_session
import account_scope
import collection_accounts

KEY = 'uid_session_renewal'
GUARD = threading.RLock()
STOP = threading.Event()
THREAD = None
ACTIVE = False
ACTIVE_ACCOUNT = None
LEAD_SECONDS = 300
RETRIES = (60, 120, 240)


def state_key():
    selected=account_scope.current()
    return KEY+':'+selected['account_id'] if selected and selected['storage']=='isolated' else KEY


def managed_accounts(*,include_disabled=False):
    with app.db() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM collection_accounts ORDER BY account_id')]
    if not rows:return [None]
    return [r for r in rows if include_disabled or r['enabled'] and set(json.loads(r['roles'])).intersection({'groups','outreach'})]


def read(c):
    row = c.execute('SELECT value FROM settings WHERE key=?', (state_key(),)).fetchone()
    return json.loads(row['value']) if row else {}


def write(value):
    with app.LOCKS['live'], app.db() as c:
        c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                  (state_key(), json.dumps(value, ensure_ascii=False)))


def state():
    with app.db() as c:
        value = read(c)
    return {k: v for k, v in value.items() if k != 'generation'}


def pending():
    return (ACTIVE and ACTIVE_ACCOUNT==state_key()) or state().get('status') in ('checking', 'retry_wait', 'attention')


def configured_sender():
    # A failed IM diagnostic must not block its own read-only recovery. Ignore
    # the send-readiness issues, but bind the exact configured provider and UID.
    selected=account_scope.current()
    if selected:
        with app.db() as c:
            row=c.execute('SELECT * FROM collection_accounts WHERE account_id=?',(selected['account_id'],)).fetchone()
            if (not row or not row['enabled'] or row['sender_uid']!=selected['sender_uid']
                    or row['storage']!=selected['storage']
                    or not set(json.loads(row['roles'])).intersection({'groups','outreach'})):
                return None
            if c.execute('SELECT 1 FROM account_login_jobs WHERE account_id=? AND finished_at IS NULL',(row['account_id'],)).fetchone():
                return None
            roles=set(json.loads(row['roles']));sender=uid_protocol.numeric_uid(row['sender_uid'])
            active=False
            if 'groups' in roles:
                active=bool(c.execute('SELECT 1 FROM monitored_groups WHERE account_uid=? AND enabled=1 LIMIT 1',(sender,)).fetchone())
                import group_discovery
                cfg=group_discovery.config(c)
                active=active or cfg.get('enabled') and cfg.get('account_uid')==sender
            if 'outreach' in roles:
                entry=c.execute("SELECT value FROM settings WHERE key='intent_outreach_policy'").fetchone()
                policy=json.loads(entry[0]) if entry else {}
                active=active or policy.get('enabled') and policy.get('sender_uid')==sender
            active=active or c.execute('SELECT 1 FROM uid_inbox_sync WHERE enabled=1 AND account_uid=? LIMIT 1',(sender,)).fetchone()
        if not active:return None
        if selected['storage']=='isolated':return sender
    settings, _ = uid_messaging.config()
    if settings.get('provider_file') != str(Path(uid_session.__file__).resolve()):
        return None
    try:
        sender = uid_protocol.numeric_uid(settings.get('sender_uid'))
    except ValueError:
        return None
    with app.db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='intent_outreach_policy'").fetchone()
        policy = json.loads(row['value']) if row else {}
        active = (settings.get('enabled') is True and policy.get('enabled') is True and policy.get('sender_uid') == sender)
        active = active or c.execute('SELECT 1 FROM uid_inbox_sync WHERE enabled=1 AND account_uid=? LIMIT 1', (sender,)).fetchone()
        active = active or c.execute('SELECT 1 FROM monitored_groups WHERE enabled=1 AND account_uid=? LIMIT 1', (sender,)).fetchone()
    return sender if active else None


def generation(data):
    return hashlib.sha256(json.dumps([data['sender_uid'], data['captured_at'],
        data.get('last_verified_at', data['captured_at'])]).encode()).hexdigest()


def tick(*,force=False):
    global ACTIVE, ACTIVE_ACCOUNT
    # Same admission order as normal service shutdown. Release GUARD during IO
    # so shutdown can promptly reject an in-flight check instead of blocking.
    with GUARD:
        if STOP.is_set() or ACTIVE:
            return
        import intent_outreach
        # An outreach tick may have selected a lead before acquiring the IM
        # guard. Do not turn that admission race into a failed send job.
        if not intent_outreach.GUARD.acquire(blocking=False):
            return
        try:
            if not uid_messaging.GUARD.acquire(blocking=False):
                return
            ACTIVE = True
            ACTIVE_ACCOUNT = state_key()
        finally:
            intent_outreach.GUARD.release()
    try:
        sender = configured_sender()
        with app.db() as c:
            previous = read(c)
        if not sender:
            if previous.get('status') in ('checking', 'retry_wait'):
                write({**previous, 'status': 'paused', 'detail': '相关监控已关闭，未继续会话续验'})
            return
        path = account_scope.directory() / 'private' / 'uid-http' / 'session.dpapi'
        try:
            data = uid_session.load(path, check_age=False)
            selected=account_scope.current()
            if data['sender_uid'] != sender or selected and data['account']!=selected['account_id']:
                raise ValueError('account mismatch')
        except Exception:
            if previous.get('status') != 'attention' or previous.get('reason') != 'session_unavailable':
                write(dict(status='attention', reason='session_unavailable', detail='本地认证缺失、格式无效或账号不匹配，需要核对', checked_at=time.time()))
            return
        key, now = generation(data), time.time()
        if previous.get('generation') != key:
            previous = {}
        if previous.get('status') == 'attention':
            return
        due = data.get('last_verified_at', data['captured_at']) + uid_session.MAX_AGE - LEAD_SECONDS
        if previous.get('status') == 'retry_wait':
            due = previous['next_run_at']
        if now < due and (not force or previous.get('status')=='retry_wait'):
            if not previous:
                write(dict(status='waiting', generation=key, next_run_at=due, failures=0,
                           detail='到期前自动进行账号身份与 IM 只读续验'))
            return
        if STOP.is_set():
            return
        checking = dict(previous, status='checking', generation=key, checked_at=now,
                        detail='正在核对原账号和 IM 会话，暂缓派发私信及收件任务')
        write(checking)
        try:
            result = uid_session.revalidate(sender, path=path, cancel=STOP)
        except RuntimeError:
            # External bootstrap owns the credential lock; no HTTP was made.
            write({**checking, 'status': 'waiting', 'detail': '本地认证正在更新，稍后重新检查'})
            return
        if result['status'] == 'im_read_verified':
            refreshed = uid_session.load(path)
            current = dict(status='waiting', generation=generation(refreshed), failures=0,
                           next_run_at=refreshed['last_verified_at'] + uid_session.MAX_AGE - LEAD_SECONDS,
                           detail='账号身份与 IM 只读续验已通过，继续原监控')
        else:
            failures = previous.get('failures', 0) + 1
            retry = result.get('transport_error') in ('timeout', 'connection_failed', 'transport_failed') and failures <= len(RETRIES) and not STOP.is_set()
            current = dict(status='retry_wait' if retry else 'attention', generation=key,
                           failures=failures, reason=result['status'],
                           detail='会话核验连接失败，将按间隔重试' if retry else '会话核验未通过，需要核对；未启动浏览器或重发消息')
            if retry:
                current['next_run_at'] = time.time() + RETRIES[failures - 1]
        finished = time.time()
        history = previous.get('history', [])[-19:] + [dict(checked_at=finished, **result)]
        write(dict(current, checked_at=finished, last_result=result, history=history))
    finally:
        with GUARD:
            ACTIVE = False
            ACTIVE_ACCOUNT = None
            uid_messaging.GUARD.release()


def recover_one():
    with GUARD, app.db() as c:
        previous = read(c)
    if previous.get('status') == 'checking':
        # A crash has no successful proof. One bounded retry follows the same
        # persisted budget; repeated restarts cannot reset it indefinitely.
        failures = previous.get('failures', 0) + 1
        retry = failures <= len(RETRIES)
        write(dict(previous, status='retry_wait' if retry else 'attention', failures=failures,
                   next_run_at=time.time() + RETRIES[min(failures, len(RETRIES)) - 1],
                   detail='上次会话续验中断，按原重试次数恢复' if retry else '会话续验多次中断，需要核对'))


def recover():
    for selected in managed_accounts(include_disabled=True):
        with account_scope.use(selected):recover_one()


def cycle():
    for selected in managed_accounts():
        if STOP.is_set():break
        with account_scope.use(selected):
            try:tick()
            except Exception:
                # A failure belongs only to its account. Other eligible accounts
                # retain their own expiry, backoff, and original saved session.
                with app.db() as c:previous=read(c)
                write(dict(previous,status='attention',reason='internal_error',checked_at=time.time(),
                           detail='该账号会话续验出现程序错误，需要核对；其他账号继续运行'))


def check_account(body,mode='live'):
    if mode!='live' or set(body)!={'account_id'} or not isinstance(body['account_id'],str):
        raise ValueError('账号核对参数无效')
    with app.db() as c:
        row=c.execute('SELECT * FROM collection_accounts WHERE account_id=?',(body['account_id'],)).fetchone()
    if not row:raise ValueError('账号不存在')
    with account_scope.use(dict(row)):
        if not configured_sender():raise ValueError('该账号没有启用消息任务，或正在登录；未发起核对')
        before=state().get('checked_at')
        # A manual check does not erase a genuine rejection or skip an existing
        # network cooldown. It only brings a healthy waiting check forward.
        tick(force=True)
        result=state()
        return dict(account_id=row['account_id'],performed=result.get('checked_at')!=before,renewal=result)


def start_service():
    global THREAD
    if THREAD and THREAD.is_alive():
        return
    STOP.clear()
    def loop():
        while not STOP.wait(10):
            try:cycle()
            except Exception:
                try:
                    with app.db() as c:previous=read(c)
                    write(dict(previous,status='attention',reason='internal_error',checked_at=time.time(),
                               detail='账号续验调度出现程序错误，需要核对'))
                except Exception:pass  # The health monitor reports an unavailable store.
    THREAD = threading.Thread(target=loop, name='uid-session-renewal', daemon=True)
    THREAD.start()


def shutdown():
    STOP.set()
    if THREAD:
        THREAD.join()
