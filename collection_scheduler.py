"""User-activated collection plans and an opt-in monitor; challenges pause dispatch."""
import json
import re
import threading
from datetime import datetime, timedelta, timezone
import clubops as app
import collector

STOP = threading.Event()
THREAD = None
GATED = {'needs_login', 'needs_verification', 'rate_limited', 'access_denied', 'session_expired', 'identity_failed'}


def verification_retry_seconds(connection, task):
    """Explicit local policy; only a finished, archived browser challenge qualifies."""
    if not task or task['status'] != 'needs_verification' or task['transport'] != 'local_browser' or not task['finished_at']:
        return 0
    try:
        policy=json.loads((app.DATA_DIR/'private/monitor-policy.json').read_text(encoding='utf-8'))
        if policy.get('captcha_retry_seconds') != 300:return 0
        row=connection.execute("SELECT snapshot FROM collection_diagnostics WHERE task_id=? AND stage='captcha_workflow' ORDER BY id DESC LIMIT 1",(task['id'],)).fetchone()
        if not row:return 0
        event=json.loads(row[0]).get('verification',{})
        if event.get('phase')!='needs_review' or event.get('reason') in ('sample_archive_unavailable','manual_mode'):return 0
        attempt=event.get('attempt_id','')
        if not re.fullmatch(r'[a-fA-F0-9-]{36}',attempt):return 0
        record=json.loads((app.DATA_DIR/'private/captcha-learning/attempts'/(attempt+'.json')).read_text(encoding='utf-8'))
        if record.get('task_id')!=task['id'] or record.get('outcome')!='needs_review' or record.get('passed') is not False or not re.fullmatch(r'[a-f0-9]{64}',record.get('case_id') or ''):return 0
        sample=json.loads((app.DATA_DIR/'private/captcha-learning/cases'/(record['case_id']+'.json')).read_text(encoding='utf-8'))
        if sample.get('latest_attempt_id')!=attempt or sample.get('latest_result')!='not_passed':return 0
        return 300
    except (OSError,ValueError,TypeError):return 0


def search_verification_only(connection, task):
    """Narrow isolation requires explicit HTTP search evidence, never a guess."""
    if task['transport'] != 'http' or task['kind'] != 'search' or task['status'] != 'needs_verification':
        return False
    found = False
    for row in connection.execute("SELECT snapshot FROM collection_diagnostics WHERE task_id=? AND stage='http_read' ORDER BY id DESC LIMIT 25", (task['id'],)):
        try:
            snapshot = json.loads(row['snapshot'])
            responses = snapshot.get('responses', [])
            if not isinstance(responses, list):
                return False
            for evidence in responses:
                if not isinstance(evidence, dict):
                    return False
                if evidence.get('status') not in GATED:
                    continue
                scoped = (evidence.get('reason') == 'search_verification_required' and evidence.get('search_nil_type') == 'verify_check'
                          or evidence.get('reason') == 'session_gate' and evidence.get('verification_scope') == 'search')
                if evidence.get('transport') != 'http' or evidence.get('operation') != 'search' or evidence.get('status') != 'needs_verification' or not scoped:
                    return False
                found = True
        except (ValueError, TypeError, AttributeError):
            return False
    return found


def state(mode='live'):
    with app.db(mode) as c:
        return [dict(r) for r in c.execute('SELECT * FROM collection_plans ORDER BY priority DESC,id DESC')]


def save(body, mode='live'):
    if mode != 'live':
        raise ValueError('演示区不创建真实采集计划')
    kind, target, videos, comments, _, _ = collector.options({**body, 'request_id': 'plan-validation', 'interactive': True})
    concurrency = min(collector.page_options(body), videos)
    transport = collector.transport_option(body)
    interval = int(body.get('interval_seconds', 600))
    priority = int(body.get('priority', 2))
    limit = int(body.get('run_limit', 3))
    name = app.clean(body.get('name'), 100) or target
    if not 300 <= interval <= 86400 or priority not in (1, 2, 3) or not 1 <= limit <= 24:
        raise ValueError('间隔需为 300–86400 秒，优先级 1–3，总批次 1–24；这些是本产品边界，不是平台安全频率保证')
    with collector.GUARD, app.LOCKS['live'], app.db() as c:
        if body.get('id'):
            row = c.execute('SELECT * FROM collection_plans WHERE id=?', (int(body['id']),)).fetchone()
            if not row or row['continuous'] or row['status'] == 'running' or row['last_task_id'] in collector.ACTIVE:
                raise ValueError('请先暂停计划并完成或停止当前任务，再修改计划')
            if limit <= row['run_count']:
                raise ValueError('总批次数必须大于已经启动的批次数；如需新一轮请创建新计划')
            if row['run_count'] and (kind, target) != (row['kind'], row['target']):
                raise ValueError('已有执行记录的计划不能更换来源目标，请新建计划')
            plan_id = row['id']
            c.execute("UPDATE collection_plans SET name=?,kind=?,target=?,video_limit=?,comment_limit=?,interval_seconds=?,priority=?,run_limit=?,status='paused',next_run_at=NULL,detail='配置已保存，等待手动启动',updated_at=? WHERE id=?", (name, kind, target, videos, comments, interval, priority, limit, app.now(), plan_id))
        else:
            count = c.execute('SELECT COUNT(*) FROM collection_plans').fetchone()[0]
            if count >= 100:
                raise ValueError('本机最多保存 100 个采集计划；请复用已有计划')
            plan_id = c.execute('INSERT INTO collection_plans(name,kind,target,video_limit,comment_limit,interval_seconds,priority,run_limit,detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (name, kind, target, videos, comments, interval, priority, limit, '已保存，尚未启动；不会访问抖音', app.now(), app.now())).lastrowid
        c.execute('UPDATE collection_plans SET page_concurrency=? WHERE id=?', (concurrency, plan_id))
        c.execute('UPDATE collection_plans SET transport=?,intent_version=intent_version+1 WHERE id=?', (transport, plan_id))
        app.event(c, 'collection-plan', f'保存有限采集计划 #{plan_id}，未启动')
    return {'id': plan_id, 'status': 'paused'}


def command(plan_id, action, mode='live', *, allow_monitor=False):
    if mode != 'live' or action not in ('start', 'pause'):
        raise ValueError('采集计划操作无效')
    with collector.GUARD, app.LOCKS['live'], app.db() as c:
        row = c.execute('SELECT * FROM collection_plans WHERE id=?', (int(plan_id),)).fetchone()
        if not row:
            raise ValueError('计划不存在')
        if row['continuous'] and not allow_monitor:
            raise ValueError('请使用监控开关操作持续监控')
        if action == 'start':
            if row['status'] == 'running':
                return {'id': row['id'], 'status': 'running'}
            if row['last_task_id'] in collector.ACTIVE:
                raise ValueError('上一批尚未关闭，请先完成或停止当前任务')
            if not row['continuous'] and row['run_count'] >= row['run_limit']:
                raise ValueError('本计划已达到总批次上限，请创建新计划')
            if row['transport'] == 'http' and row['kind'] == 'search':
                import collector_http_session
                session = collector_http_session.status(app.DATA_DIR)
                if session.get('endpoints', {}).get('search') == 'needs_verification':
                    raise ValueError('当前 HTTP 搜索会话仍待人工验证；指定视频成功不会解除搜索暂停')
            latest = c.execute('SELECT * FROM collection_tasks WHERE transport=? ORDER BY id DESC LIMIT 1', (row['transport'],)).fetchone()
            observed = sum(latest[k] for k in ('comments', 'filtered_old', 'filtered_unknown', 'filtered_future', 'filtered_keyword', 'filtered_blocked')) if latest else 0
            healthy = bool(latest and latest['status'] == 'completed' and observed > 0)
            if row['continuous'] and latest and latest['id'] == row['last_task_id'] and latest['status'] in ('completed', 'cancelled'):
                # A deliberate stop or an empty successful monitor batch does not
                # erase its previously proven baseline. Failed/gated tasks do.
                healthy = bool(c.execute("SELECT 1 FROM collection_tasks WHERE transport=? AND status='completed' AND comments+filtered_old+filtered_unknown+filtered_future+filtered_keyword+filtered_blocked>0 LIMIT 1", (row['transport'],)).fetchone())
            retry = verification_retry_seconds(c,latest) if row['continuous'] and latest and (row['kind'],row['target'])==(latest['kind'],latest['target']) else 0
            if retry:
                healthy = bool(c.execute("SELECT 1 FROM collection_tasks WHERE transport=? AND status='completed' AND comments+filtered_old+filtered_unknown+filtered_future+filtered_keyword+filtered_blocked>0 LIMIT 1",(row['transport'],)).fetchone())
            if not healthy:
                raise ValueError('请先手动完成一批实际读到评论的采集，再启用持续计划；目前尚未验证或最近一批未正常完成')
            status, due = 'running', app.now()
            if retry:
                due=max(datetime.fromisoformat(due),datetime.fromisoformat(latest['finished_at'])+timedelta(seconds=retry)).isoformat(timespec='seconds')
            detail = '监控已开启；按批检查滚动时间范围，直到关闭或遇到需要处理的状态' if row['continuous'] else '已启用有限调度；空闲时按优先级执行，遇到验证或失败暂停'
        else:
            status, due = 'paused', None
            detail = '监控已关闭；已阻止后续批次，保留已有数据' if row['continuous'] else '已暂停后续批次；如需结束当前浏览器任务，请使用任务的停止按钮'
        c.execute('UPDATE collection_plans SET status=?,detail=?,next_run_at=?,activated_at=CASE WHEN ?=\'running\' THEN ? ELSE activated_at END,updated_at=?,intent_version=intent_version+1 WHERE id=?', (status, detail, due, status, app.now(), app.now(), row['id']))
        app.event(c, 'collection-plan', f'计划 #{row["id"]}：{detail}')
    return {'id': row['id'], 'status': status}


def recover():
    with app.LOCKS['live'], app.db() as c:
        # Invalidate any in-memory login continuation, including attention plans.
        c.execute('UPDATE collection_plans SET intent_version=intent_version+1')
        c.execute("UPDATE collection_plans SET status='paused',next_run_at=NULL,detail='服务重启，计划已暂停；请检查结果后手动启用',updated_at=? WHERE status='running'", (app.now(),))


def login_context(task_id):
    """Capture existing plan intent under collector.GUARD; never enable a plan."""
    with app.db() as c:
        rows = c.execute('SELECT id,intent_version,status,activated_at FROM collection_plans WHERE last_task_id=?', (task_id,)).fetchall()
    if len(rows) > 1:
        raise ValueError('采集任务对应多个计划，无法自动恢复')
    if not rows:
        return None
    row = rows[0]
    return {'id': row['id'], 'task_id': task_id, 'intent_version': row['intent_version'],
            'continue_plan': row['status'] in ('running', 'attention') and bool(row['activated_at'])}


def attach_login_continuation(c, context, child_id):
    """Bind the resumed task inside its creation transaction, before dispatch."""
    if context is None:
        return
    row = c.execute('SELECT * FROM collection_plans WHERE id=?', (context['id'],)).fetchone()
    if (not row or row['last_task_id'] != context['task_id'] or row['intent_version'] != context['intent_version']
            or context['continue_plan'] and row['status'] not in ('running', 'attention')):
        raise ValueError('监控或计划已关闭、修改或重启；登录完成后不自动采集')
    if not context['continue_plan']:
        return  # A manual one-batch recovery never enables a paused plan.
    settled = int(row['settled_task_id'] == context['task_id'])
    if row['run_count'] < 1 or row['settled_count'] < settled:
        raise ValueError('采集计划计数无效，无法自动恢复')
    # A login continuation finishes the same scheduled batch, not a new run.
    # The failed attempt remains intact in collection_tasks and login history.
    c.execute("""UPDATE collection_plans SET last_task_id=?,settled_count=settled_count-?,
        status='running',next_run_at=NULL,detail=?,updated_at=? WHERE id=?""",
        (child_id, settled, f'登录已恢复，任务 #{child_id} 接续原批次；完成后按原间隔调度', app.now(), row['id']))
    app.event(c, 'collection-plan', f'计划 #{row["id"]} 的任务 #{context["task_id"]} 经登录恢复接续至 #{child_id}')


def tick(instant=None):
    instant = instant or app.now()
    with collector.GUARD:
        import login_recovery
        if login_recovery.busy():
            return None  # Login owns the account; do not turn waiting into failure.
        with app.LOCKS['live'], app.db() as c:
            # Only proven search-specific verification permits unrelated video plans.
            recent = c.execute('SELECT * FROM collection_tasks ORDER BY id DESC LIMIT 1').fetchone()
            if recent and recent['status'] in GATED:
                if verification_retry_seconds(c,recent):
                    c.execute("UPDATE collection_plans SET status='attention',next_run_at=NULL,detail=?,updated_at=? WHERE status='running' AND NOT (continuous=1 AND transport=? AND kind=? AND target=?)",(f'任务 #{recent["id"]} 遇到验证；其他计划暂停',instant,recent['transport'],recent['kind'],recent['target']))
                elif search_verification_only(c, recent):
                    c.execute("UPDATE collection_plans SET status='attention',next_run_at=NULL,detail=?,updated_at=? WHERE status='running' AND (kind='search' OR transport!='http')", (f'任务 #{recent["id"]} 的 HTTP 搜索待人工验证，搜索及浏览器计划已暂停；已启用的指定视频 HTTP 计划独立执行', instant))
                else:
                    c.execute("UPDATE collection_plans SET status='attention',next_run_at=NULL,detail=?,updated_at=? WHERE status='running'", (f'任务 #{recent["id"]} 遇到 {recent["status"]}，本机运行计划已暂停，不自动重试', instant))
            plans = c.execute('SELECT * FROM collection_plans WHERE last_task_id IS NOT NULL AND (settled_task_id IS NULL OR settled_task_id!=last_task_id)').fetchall()
            for p in plans:
                task = c.execute('SELECT * FROM collection_tasks WHERE id=?', (p['last_task_id'],)).fetchone()
                if not task or not task['finished_at'] or task['id'] in collector.ACTIVE:
                    continue
                new_status, detail, due = p['status'], '上批已结束', None
                if p['status'] == 'paused':
                    detail = p['detail']
                elif p['continuous'] and p['status']=='running' and verification_retry_seconds(c,task):
                    due=(datetime.fromisoformat(task['finished_at'])+timedelta(seconds=300)).astimezone(timezone.utc).isoformat(timespec='seconds')
                    detail='验证码未通过，样本已记录；暂停 5 分钟后在后台重新加载原监控目标。可随时关闭监控。'
                elif task['status'] != 'completed':
                    new_status, detail = 'attention', f'上批结果 {task["status"]}：{task["detail"]}；计划停止自动执行'
                elif not p['continuous'] and p['run_count'] >= p['run_limit']:
                    new_status, detail = 'completed', '已达到本计划总批次上限，不再自动请求'
                elif p['status'] == 'running':
                    finished = datetime.fromisoformat(task['finished_at'])
                    due = (finished + timedelta(seconds=p['interval_seconds'])).astimezone(timezone.utc).isoformat(timespec='seconds')
                    detail = '上一批完成，等待下一批；间隔不代表平台认可的请求频率'
                c.execute('UPDATE collection_plans SET settled_count=settled_count+1,settled_task_id=?,status=?,detail=?,next_run_at=?,updated_at=? WHERE id=?', (task['id'], new_status, detail, due, instant, p['id']))
            if collector.ACTIVE:
                return None
            # Stored timestamps may use UTC or +08:00. Compare instants, never ISO strings.
            plan = c.execute("SELECT * FROM collection_plans WHERE status='running' AND (continuous=1 OR run_count<run_limit) AND julianday(next_run_at)<=julianday(?) AND (last_task_id IS NULL OR settled_task_id=last_task_id) ORDER BY priority DESC,julianday(next_run_at),id LIMIT 1", (instant,)).fetchone()
        if not plan:
            return None
        try:
            # Scheduled reads never raise a window; explicit one-off tasks may.
            result = collector.start({'kind': plan['kind'], 'target': plan['target'], 'transport': plan['transport'], 'video_limit': plan['video_limit'], 'comment_limit': plan['comment_limit'], 'page_concurrency': plan['page_concurrency'], 'interactive': False, 'request_id': f'plan-{plan["id"]}-run-{plan["run_count"]+1}'}, lookback_hours=plan['lookback_hours'], include_keywords=plan['include_keywords'], exclude_keywords=plan['exclude_keywords'])
            with app.LOCKS['live'], app.db() as c:
                c.execute('UPDATE collection_plans SET run_count=run_count+1,last_task_id=?,next_run_at=NULL,detail=?,updated_at=? WHERE id=?', (result['id'], f'正在执行任务 #{result["id"]}', instant, plan['id']))
            return result['id']
        except Exception as exc:
            with app.LOCKS['live'], app.db() as c:
                c.execute("UPDATE collection_plans SET status='attention',next_run_at=NULL,detail=?,updated_at=? WHERE id=?", ('任务派发失败：'+type(exc).__name__+'；请检查后手动恢复', instant, plan['id']))
            return None


def start_service():
    global THREAD
    if THREAD and THREAD.is_alive():
        return
    STOP.clear()
    def loop():
        while not STOP.wait(3):
            try:
                tick()
            except Exception:
                # Do not keep retrying unknown failures against the platform.
                recover()
                STOP.set()
    THREAD = threading.Thread(target=loop, name='collection-scheduler', daemon=True)
    THREAD.start()


def shutdown():
    STOP.set()
    if THREAD:
        THREAD.join(timeout=4)
    recover()
