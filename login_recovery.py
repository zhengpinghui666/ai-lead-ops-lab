"""One bounded own-account login job; no OTP is persisted or logged."""
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid

import clubops as app
import collector_http_session as sessions
import login_relay
import runtime
import uid_bootstrap

GUARD = threading.RLock()
ACTIVE = None
BASE = Path(__file__).resolve().parent
CONFIG = 'login-recovery.json'
PHASES = {'opening_browser', 'arming_relay', 'waiting_sms', 'code_received', 'code_filled',
          'checking_browser', 'checking_identity', 'manual_required', 'cancelling', 'resuming'}
FINISHED = {'completed', 'cancelled', 'timeout', 'account_mismatch', 'relay_unavailable',
            'browser_failed', 'identity_failed', 'interrupted', 'resume_pending', 'phone_tested'}
LOGIN_FAILURES = {'needs_login', 'session_expired', 'identity_failed'}
DEFAULTS = {'account': '1267597446', 'auto_recover': False}


def _file(name):
    return app.DATA_DIR / 'private' / 'login-recovery' / name


def _read(path, default):
    try:
        if path.is_symlink() or path.stat().st_size > 32768:
            raise ValueError('登录配置文件无效')
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('登录配置路径无效')
    temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def config():
    value = _read(app.DATA_DIR / CONFIG, DEFAULTS)
    if (not isinstance(value, dict) or set(value) != set(DEFAULTS)
            or not isinstance(value['account'], str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]{2,64}', value['account'])
            or type(value['auto_recover']) is not bool):
        raise ValueError('登录恢复配置无效')
    return value


def checks():
    value = _read(_file('checks.json'), {})
    paired = login_relay.state()
    return value if isinstance(value, dict) and value.get('origin') == paired['origin'] else {}


def _history():
    rows = _read(_file('jobs.json'), [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('登录恢复记录无效')
    return rows


def state(mode='live'):
    if mode != 'live':
        return {'available': False, 'active': False}
    with GUARD:
        try:
            import collector
            with app.db() as connection:
                candidates = [dict(row) for row in connection.execute("""SELECT id,kind,target,status,finished_at
                    FROM collection_tasks WHERE finished_at IS NOT NULL AND
                    ((transport='http' AND status IN ('needs_login','session_expired','identity_failed'))
                     OR (transport='local_browser' AND status='needs_login')) ORDER BY id DESC LIMIT 20""")]
            result = {'available': True, 'active': ACTIVE is not None, 'config': config(),
                      'relay': login_relay.state(), 'checks': checks(),
                      'session': sessions.status(app.DATA_DIR), 'history': _history()[-10:],
                      'collection_busy': bool(collector.ACTIVE), 'resume_candidates': candidates}
        except (ValueError, OSError, TypeError):
            # A broken recovery file must not take down the whole workbench.
            result = {'available': False, 'active': ACTIVE is not None,
                      'error': '登录恢复配置无法读取，请检查本机配置文件', 'history': []}
        if ACTIVE is not None:
            result['job'] = dict(ACTIVE['public'])
            if ACTIVE['public']['kind'] == 'phone_test':
                # A synthetic pairing message, never a real SMS login code.
                result['test_message'] = '抖音连接测试，验证码：' + ACTIVE['test_code']
        return result


def busy():
    with GUARD:
        return ACTIVE is not None


def save(body):
    if not isinstance(body, dict) or set(body) != set(DEFAULTS):
        raise ValueError('仅接受登录账号和自动恢复开关')
    if (not isinstance(body['account'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{2,64}', body['account'])
            or type(body['auto_recover']) is not bool):
        raise ValueError('登录恢复参数无效')
    with GUARD:
        if ACTIVE:
            raise ValueError('请先结束当前登录任务再修改配置')
        evidence = checks()
        if body['auto_recover'] and not (login_relay.state()['ready'] and evidence.get('health_at') and evidence.get('phone_test_at')):
            raise ValueError('请先完成中转检查和手机测试，再开启自动恢复')
        _write(app.DATA_DIR / CONFIG, dict(body))
    return state()


def check_relay():
    paired = login_relay.state()
    if not paired['ready']:
        raise ValueError('请先部署并绑定中转地址')
    result = login_relay.Relay(paired['origin']).call('health')
    if result != {'service': 'clubops-login-relay', 'version': 1}:
        raise ValueError('中转服务版本不匹配')
    with GUARD:
        _write(_file('checks.json'), {**checks(), 'origin': paired['origin'], 'health_at': time.time()})
    return state()


def _phase(control, phase):
    if phase not in PHASES | FINISHED:
        phase = 'browser_failed'
    with GUARD:
        if control['cancel'].is_set() and phase not in FINISHED:
            phase = 'cancelling'
        control['public'].update(status=phase, updated_at=time.time())
        history = _history()
        history = [row for row in history if row.get('id') != control['public']['id']]
        _write(_file('jobs.json'), (history + [dict(control['public'])])[-30:])


def _task(task_id):
    import collector
    if type(task_id) is not int or task_id < 1:
        raise ValueError('采集任务 ID 无效')
    with app.db() as connection:
        row = connection.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
    eligible = row and ((row['transport'] == 'http' and row['status'] in LOGIN_FAILURES)
                        or (row['transport'] == 'local_browser' and row['status'] == 'needs_login'))
    if not eligible or not row['finished_at'] or task_id in collector.ACTIVE:
        raise ValueError('只恢复因未登录或身份失效结束的 HTTP 任务，或明确要求登录的浏览器采集任务')
    return dict(row)


def start(body, *, automatic=False):
    global ACTIVE
    import collector
    if not isinstance(body, dict) or set(body) - {'kind', 'task_id'}:
        raise ValueError('登录任务参数无效')
    kind = body.get('kind', 'login')
    if kind not in ('login', 'phone_test') or kind == 'phone_test' and body.get('task_id') is not None:
        raise ValueError('登录任务类型无效')
    with collector.GUARD, GUARD:
        if ACTIVE or collector.ACTIVE:
            raise ValueError('已有登录或采集任务，请先结束当前任务')
        task = _task(body['task_id']) if body.get('task_id') is not None else None
        import collection_scheduler
        plan_context = collection_scheduler.login_context(task['id']) if task else None
        settings = config()
        if not login_relay.state()['ready']:
            raise ValueError('请先部署并绑定短信中转地址')
        if automatic:
            if not settings['auto_recover'] or not task:
                return None
            if plan_context and not plan_context['continue_plan']:
                return None
            history = _history()
            if any(row.get('task_id') == task['id'] or row.get('resumed_task_id') == task['id'] for row in history):
                return None
            if history and time.time() - history[-1].get('updated_at', 0) < 300:
                return None
        job_id = uuid.uuid4().hex
        control = {'public': {'id': job_id, 'kind': kind, 'account': settings['account'],
                              'task_id': task['id'] if task else None, 'status': 'opening_browser',
                              'created_at': time.time(), 'updated_at': time.time()},
                   'cancel': threading.Event(), 'manual': threading.Event(), 'task': task, 'plan_context': plan_context,
                   'test_code': f'{secrets.randbelow(1000000):06d}' if kind == 'phone_test' else '',
                   'process': None, 'thread': None}
        ACTIVE = control
        try:
            _phase(control, 'arming_relay' if kind == 'phone_test' else 'checking_identity')
            thread = threading.Thread(target=_run, args=(control,), daemon=True, name='login-recovery')
            control['thread'] = thread
            # Admissions and shutdown cannot observe a not-yet-started thread.
            thread.start()
        except Exception:
            ACTIVE = None
            control['test_code'] = ''
            _phase(control, 'browser_failed')
            raise ValueError('登录恢复任务未能启动，请稍后重试') from None
    return dict(control['public'])


def command(body, action):
    if set(body) != {'id'} or action not in ('cancel', 'complete'):
        raise ValueError('登录操作参数无效')
    with GUARD:
        if not ACTIVE or ACTIVE['public']['id'] != body['id']:
            raise ValueError('登录任务已结束或发生变化')
        if action == 'complete':
            if ACTIVE['public']['kind'] != 'login' or ACTIVE['public']['status'] not in {'waiting_sms', 'manual_required', 'checking_browser', 'code_filled'}:
                raise ValueError('当前没有等待手动完成的登录页面')
            ACTIVE['manual'].set()
        else:
            ACTIVE['cancel'].set()
            _phase(ACTIVE, 'cancelling')
    return state()


def _reuse(account):
    status = sessions.status(app.DATA_DIR)
    if not status['ready'] or status.get('account') != account:
        return False
    value = sessions.load(app.DATA_DIR)
    result = uid_bootstrap.probe({'expected_account': account,
        'cookie': sessions.cookie_header(value, 'identity'), 'user_agent': value['user_agent']})
    matched = result['status'] == 'identity_verified' and result.get('sender_uid') == value.get('sender_uid')
    sessions.record_identity_status(value, 'identity_verified' if matched else 'identity_failed', app.DATA_DIR)
    return matched


def _send(process, value):
    process.stdin.write(json.dumps(value) + '\n')
    process.stdin.flush()


def _identity_diagnostic(event):
    clean = {}
    for key, allowed in (
        ('preparation_status', {'session_ready','invalid_input','identity_failed','needs_login','account_mismatch','browser_identity_unverified','needs_login_or_verification','bootstrap_result_unknown','invalid_result','dependency_missing','bootstrap_failed'}),
        ('bootstrap_phase', {'validation','cookie_header','identity_http','credential_save','complete'}),
        ('bootstrap_error', {'invalid_structure','invalid_metadata','invalid_account','local_expiry','missing_cookie_scopes','invalid_cookie_list','invalid_cookie','invalid_cookie_value','cookie_domain_mismatch','cookie_path_mismatch','invalid_cookie_expiry','invalid_client_context','credential_path_unwritable','preparation_failed'})):
        if isinstance(event.get(key), str) and event[key] in allowed:
            clean[key] = event[key]
    if event.get('identity_status') in uid_bootstrap.DETAILS:
        clean['identity_status'] = event['identity_status']
    evidence = event.get('identity_evidence')
    if isinstance(evidence, dict):
        output = {}
        for key, low, high in (('http_status',100,599), ('response_bytes',0,262145), ('business_code',-2**31,2**31-1)):
            if type(evidence.get(key)) is int and low <= evidence[key] <= high:
                output[key] = evidence[key]
        for key in ('user_present','verification_indicated'):
            if type(evidence.get(key)) is bool:
                output[key] = evidence[key]
        if isinstance(evidence.get('response_sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', evidence['response_sha256']):
            output['response_sha256'] = evidence['response_sha256']
        for key, allowed in (('transport_phase', {'connect','request','response_headers','response_body'}),
                             ('transport_error', {'connection_failed','tls_verification_failed','invalid_response','response_exceeds_bound','transport_failed','timeout'})):
            if isinstance(evidence.get(key), str) and evidence[key] in allowed:
                output[key] = evidence[key]
        if output:
            clean['identity_evidence'] = output
    for key in ('browser_closed_before_http','credential_file_created','browser_identity_matched','sms_step_used','code_filled'):
        if type(event.get(key)) is bool:
            clean[key] = event[key]
    return clean


def _resume_task(control):
    import collector
    task = control['task']
    request_id = 'LOGIN-RECOVERY-' + control['public']['id']
    with app.db() as connection:
        pending = connection.execute("SELECT 1 FROM collection_checkpoints WHERE task_id=? AND status NOT IN ('done','unavailable') LIMIT 1", (task['id'],)).fetchone()
        import discovery_tracking
        discovery_job = discovery_tracking.worker_config(connection, task['id'])
    if pending:
        return collector.resume(task['id'], request_id, recovery_plan=control.get('plan_context'), discovery_job=discovery_job)
    # Preserve the original absolute time cutoff, even if login took minutes.
    body = {key: task[key] for key in ('kind', 'target', 'video_limit', 'comment_limit', 'page_concurrency', 'transport')}
    body.update(interactive=bool(task['interactive']), request_id=request_id)
    return collector.start(body, lookback_hours=task['lookback_hours'], include_keywords=task['include_keywords'],
                           exclude_keywords=task['exclude_keywords'], recovery_since=task['comment_since'],
                           recovery_plan=control.get('plan_context'), discovery_job=discovery_job)


def _run(control):
    global ACTIVE
    remote = None
    process = None
    armed = False
    relay_started = False
    reader_done = threading.Event()
    result = 'browser_failed'
    job = control['public']
    try:
        if job['kind'] == 'login' and not control['task'] and _reuse(job['account']):
            result = 'completed'
            return
        remote = login_relay.Relay(login_relay.state()['origin'])
        events = queue.Queue(maxsize=80)
        if job['kind'] == 'login':
            node, package = runtime.dependencies()
            if not node or not package.is_dir():
                raise ValueError('登录浏览器依赖缺失')
            environment = {**os.environ, 'CLUBOPS_PLAYWRIGHT': str(package), 'CLUBOPS_DATA_DIR': str(app.DATA_DIR),
                           'CLUBOPS_PYTHON': sys.executable, 'PYTHONIOENCODING': 'utf-8'}
            process = subprocess.Popen([node, str(BASE / 'scripts/login-recovery.cjs')], cwd=BASE,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding='utf-8',
                text=True, bufsize=1, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            control['process'] = process
            def reader():
                try:
                    for line in iter(lambda: process.stdout.readline(4097), ''):
                        if len(line) > 4096:
                            raise ValueError('output_limit')
                        message = json.loads(line)
                        # Never copy unknown fields or raw browser diagnostics.
                        if message.get('type') in ('status', 'result'):
                            events.put_nowait({**{key: message[key] for key in ('type', 'status') if key in message},
                                               **_identity_diagnostic(message)})
                except Exception:
                    control['cancel'].set()
                finally:
                    reader_done.set()
            threading.Thread(target=reader, daemon=True, name='login-output').start()
            _send(process, {'command': 'start', 'account': job['account']})
        else:
            events.put({'type': 'status', 'status': 'arming_relay'})
        deadline = time.monotonic() + (610 if process else 190)
        next_poll = 0.0
        relay_deadline = 0.0
        delivered = False
        while time.monotonic() < deadline:
            if control['cancel'].is_set():
                result = 'cancelled'
                break
            if process and control['manual'].is_set():
                control['manual'].clear()
                _send(process, {'command': 'complete'})
            try:
                event = events.get(timeout=.15)
            except queue.Empty:
                event = None
            if event:
                phase = event.get('status')
                if event['type'] == 'result':
                    control['public'].update(_identity_diagnostic(event))
                    result = phase if phase in FINISHED else 'identity_failed'
                    if result == 'completed' and not (event.get('browser_closed_before_http') and event.get('credential_file_created')):
                        result = 'identity_failed'
                    break
                if phase not in PHASES:
                    raise ValueError('登录状态无效')
                _phase(control, phase)
                if phase == 'arming_relay' and not armed:
                    relay_started = True
                    arm_started = time.monotonic()
                    created = remote.call('login', {'id': job['id'], 'account': job['account']})
                    remaining = created.get('expires_in_seconds')
                    if control['cancel'].is_set():
                        result = 'cancelled'
                        break
                    if (created.get('id') != job['id'] or type(remaining) not in (int, float)
                            or not math.isfinite(remaining) or not 0 <= remaining <= 180):
                        raise ValueError('中转登录任务不匹配')
                    relay_deadline = arm_started + remaining
                    if time.monotonic() >= relay_deadline:
                        result = 'timeout'
                        break
                    armed = True
                    if process:
                        _send(process, {'command': 'ready'})
                    _phase(control, 'waiting_sms')
            if armed and not delivered and time.monotonic() >= next_poll:
                if time.monotonic() >= relay_deadline:
                    _phase(control, 'manual_required')
                    delivered = True
                    if not process:
                        result = 'timeout'
                        break
                else:
                    try:
                        payload = remote.call('take', {'id': job['id']})
                    except login_relay.RelayError as exc:
                        control['public']['relay_error'] = exc.diagnostic()
                        if not process:
                            raise
                        # Keep the same page available for manual completion.
                        delivered = True
                        _phase(control, 'manual_required')
                        continue
                    if control['cancel'].is_set():
                        result = 'cancelled'
                        break
                    if time.monotonic() >= relay_deadline:
                        # Do not fill a response that arrived after our bounded
                        # lifetime. The next iteration settles expiry normally.
                        continue
                    status = payload.get('status')
                    if status == 'received':
                        code = payload.pop('code', None)
                        if payload.get('id') != job['id'] or not isinstance(code, str) or not re.fullmatch(r'\d{4,8}', code):
                            raise ValueError('中转返回无效验证码')
                        delivered = True
                        if process:
                            _send(process, {'command': 'otp', 'code': code})
                            _phase(control, 'code_received')
                        elif secrets.compare_digest(code, control['test_code']):
                            with GUARD:
                                _write(_file('checks.json'), {**checks(), 'origin': remote.origin, 'phone_test_at': time.time()})
                            result = 'phone_tested'
                            break
                        else:
                            result = 'identity_failed'
                            break
                        code = None
                    elif status == 'taken':
                        delivered = True
                        _phase(control, 'manual_required')
                        if not process:
                            result = 'identity_failed'
                            break
                    elif status != 'waiting':
                        raise ValueError('中转返回无效状态')
                    next_poll = time.monotonic() + 2
            if process and process.poll() is not None and reader_done.is_set() and events.empty():
                break
        else:
            result = 'timeout'
    except login_relay.RelayError as exc:
        control['public']['relay_error'] = exc.diagnostic()
        result = 'relay_unavailable'
    except Exception:
        result = 'browser_failed'
    finally:
        if process:
            try:
                if process.poll() is None:
                    _send(process, {'command': 'cancel'})
                    process.wait(timeout=12)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                # Bound only the helper this job owns, never another service.
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    control['public']['helper_cleanup_pending'] = True
            finally:
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        if relay_started and remote:
            try:
                remote.call('cancel', {'id': job['id']})
            except login_relay.RelayError as exc:
                # Remote TTL remains the fallback; retain the fact, not its body.
                control['public']['relay_cleanup_pending'] = True
                control['public']['relay_cleanup_error'] = exc.diagnostic()
        import collector
        # Hold admission while releasing login ownership and binding the child.
        # Stop/edit and scheduler dispatch use the same outer lock.
        with collector.GUARD, GUARD:
            if control['cancel'].is_set():
                result = 'cancelled'
            if ACTIVE is control:
                ACTIVE = None
            control['test_code'] = ''
            _phase(control, result)
            if result == 'completed' and control['task']:
                try:
                    _phase(control, 'resuming')
                    resumed = _resume_task(control)
                    control['public']['resumed_task_id'] = resumed['id']
                    _phase(control, 'completed')
                except Exception:
                    _phase(control, 'resume_pending')


def after_collection(task_id):
    try:
        if config()['auto_recover']:
            start({'task_id': task_id}, automatic=True)
    except (ValueError, OSError):
        pass  # The original task and its failure remain visible and retryable.


def recover():
    # Restart does not reopen a browser or request another code.
    with GUARD:
        try:
            history = _history()
        except (ValueError, OSError, TypeError):
            return  # Public state shows the local error; other work stays usable.
        changed = False
        for job in history:
            if job.get('status') not in FINISHED:
                job.update(status='interrupted', updated_at=time.time())
                changed = True
        if changed:
            _write(_file('jobs.json'), history)


def shutdown():
    with GUARD:
        control = ACTIVE
        if control:
            control['cancel'].set()
    if control and control.get('thread'):
        control['thread'].join(timeout=30)
