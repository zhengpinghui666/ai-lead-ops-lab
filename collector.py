"""Bounded collector with explicitly selected browser or backend HTTP transport."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import clubops as app
import comment_filters
import captcha_runtime
import runtime
import candidate_pool

BASE = Path(__file__).resolve().parent
GUARD = threading.RLock()
ACTIVE = {}  # Processes are ephemeral; task outcomes and observations are durable.
WAITING = {'needs_login', 'needs_verification', 'needs_interaction'}
TERMINAL = {'completed', 'partial', 'failed', 'cancelled', 'interrupted', 'rate_limited', 'access_denied', 'no_data', 'dependency_missing', 'session_expired', 'network_error', 'schema_changed', 'timeout', 'resource_limited', 'empty_response', 'identity_failed', 'upstream_rejected'}


def transport_option(body):
    value = body.get('transport', 'local_browser')
    if value not in ('http', 'local_browser'):
        raise ValueError('采集通道须为 HTTP 或本机浏览器')
    return value


def canonical_video(value):
    value = str(value or '').strip()
    if re.fullmatch(r'\d{5,30}', value):
        return 'https://www.douyin.com/video/' + value
    u = urlparse(value)
    if u.scheme != 'https' or u.hostname not in {'www.douyin.com', 'douyin.com'} or u.port not in (None, 443) or u.username or u.password:
        raise ValueError('请使用抖音完整视频链接 https://www.douyin.com/video/视频ID 或原始数字视频 ID；暂不解析短链')
    match = re.fullmatch(r'/video/(\d{5,30})/?', u.path)
    if not match:
        raise ValueError('需要完整的 /video/视频ID 链接')
    return 'https://www.douyin.com/video/' + match.group(1)


def video_targets(value):
    """One bounded, ordered video pool; canonical identities remove duplicates."""
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError('指定视频请每行填写一个完整链接或数字 ID，最多 5 个视频')
    urls = list(dict.fromkeys(canonical_video(line.strip()) for line in value.splitlines() if line.strip()))
    if not 1 <= len(urls) <= 5:
        raise ValueError('指定视频请每行填写一个完整链接或数字 ID，最多 5 个视频')
    return [{'video_id': url.rsplit('/', 1)[1], 'video_title': url.rsplit('/', 1)[1], 'video_url': url} for url in urls]


def options(body):
    kind = body.get('kind', 'search')
    if kind not in ('search', 'video', 'author'):
        raise ValueError('采集类型无效')
    target = str(body.get('target', '')).strip()
    if kind in ('video', 'author'):
        targets = video_targets(target)
        if kind == 'author' and (len(targets) > 3 or transport_option(body) != 'http'):
            raise ValueError('作者作品发现仅支持后端 HTTP；每行一个种子作品链接或 ID，最多 3 个')
        if kind == 'video' and len(targets) > 1 and transport_option(body) != 'http':
            raise ValueError('多个指定视频请使用后端 HTTP 通道；本机浏览器只接受一个指定视频')
        target = '\n'.join(row['video_url'] for row in targets)
    elif not 1 <= len(target) <= 80 or any(ord(c) < 32 for c in target):
        raise ValueError('请填写 1–80 字的搜索关键词')
    videos, comments = int(body.get('video_limit', 3)), int(body.get('comment_limit', 30))
    if not 1 <= videos <= 5 or not 1 <= comments <= 100:
        raise ValueError('单批支持 1–5 个视频，每视频最多 1–100 条已加载评论')
    interactive = body.get('interactive', True)
    if not isinstance(interactive, bool):
        raise ValueError('interactive 必须为布尔值')
    request_id = app.clean(body.get('request_id'), 100)
    if not request_id:
        raise ValueError('缺少幂等请求 ID')
    return kind, target, len(targets) if kind == 'video' else videos, comments, int(interactive), request_id


def dependencies():
    return runtime.dependencies()


def page_options(body):
    value = body.get('page_concurrency', 1)
    if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and value.isdigit()):
        raise ValueError('视频读取并发必须为 1–4 的整数')
    value = int(value)
    if not 1 <= value <= 4:
        raise ValueError('视频读取并发范围为 1–4；不是平台安全频率保证')
    return value


def state(mode='live'):
    import collector_http
    node, package = dependencies()
    with app.db(mode) as c:
        candidates = candidate_pool.state(c)
        tasks = [dict(r) for r in c.execute('SELECT * FROM collection_tasks ORDER BY id DESC LIMIT 30')]
        source = c.execute("SELECT * FROM sources WHERE kind='browser' ORDER BY id LIMIT 1").fetchone()
        for task in tasks:
            verification = [json.loads(r[0])['verification'] for r in c.execute(
                "SELECT snapshot FROM collection_diagnostics WHERE task_id=? AND stage='captcha_workflow' ORDER BY id", (task['id'],))]
            task['verification'] = verification[-1] if verification else None
            task['verification_counts'] = {
                # submissions is a cumulative per-batch counter. A read-only
                # resume has a new event ID but must not count as another input.
                'attempted': max((v.get('submissions', 0) for v in verification), default=0),
                'recovered': len({v['attempt_id'] for v in verification if v['phase'] == 'accepted'})}
            task['checkpoints'] = [dict(r) for r in c.execute('SELECT * FROM collection_checkpoints WHERE task_id=? ORDER BY rowid', (task['id'],))]
            parent = c.execute('SELECT parent_task_id FROM collection_resumes WHERE task_id=?', (task['id'],)).fetchone()
            task['parent_task_id'] = parent[0] if parent else None
            task['resumable'] = bool(task['finished_at'] and any(r['status'] not in ('done','unavailable') for r in task['checkpoints']))
            analysis = c.execute("SELECT COUNT(*) AS total,COALESCE(SUM(x.analysis_method='pending'),0) AS pending FROM comments x JOIN collection_observations o ON o.external_id=x.external_id WHERE o.task_id=? AND o.kind='comment' AND o.filter_reason='' AND x.source_id=?", (task['id'], source['id'] if source else -1)).fetchone()
            task['analysis'] = {'status': 'no_comments' if not analysis['total'] else 'pending' if analysis['pending'] else 'completed',
                'total': analysis['total'], 'pending': analysis['pending'], 'analyzed': analysis['total'] - analysis['pending']}
    with GUARD:
        for task in tasks:
            task['active'] = task['id'] in ACTIVE if mode == 'live' else False
    http_state = collector_http.runtime_status(app.DATA_DIR)
    http_state['live_verified'] = any(t['transport'] == 'http' and t['status'] in ('completed','partial') and t['comments'] > 0 for t in tasks)
    return {'available': bool(shutil.which(node) or Path(node).is_file()) and package.is_dir(), 'tasks': tasks,
            'source_id': source['id'] if source else None, 'last_received': source['last_received'] if source else None,
            'transport': 'selectable', 'http': http_state, 'verification': captcha_runtime.status(),
            'mode': 'bounded_batch', 'external_sender': False, 'candidate_pool': candidates}


def record_verification(task_id, value):
    event = captcha_runtime.clean_event(value)
    with app.LOCKS['live'], app.db() as c:
        task = c.execute('SELECT transport,finished_at FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
        if not task or task['finished_at'] or task['transport'] != event['transport']:
            raise ValueError('验证码事件与任务不匹配')
        count = c.execute("SELECT COUNT(*) FROM collection_diagnostics WHERE task_id=? AND stage='captcha_workflow'", (task_id,)).fetchone()[0]
        if count < 30:
            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)',
                      (task_id, 'captcha_workflow', json.dumps({'verification': event}), app.now()))


def update(task_id, **values):
    allowed = {'status', 'detail', 'videos', 'comments', 'inserted', 'duplicate', 'revised', 'skipped', 'page_url', 'finished_at', 'active_pages'}
    if not values or not set(values) <= allowed:
        raise ValueError('任务字段无效')
    with app.LOCKS['live'], app.db() as c:
        if values.get('status') in TERMINAL:
            values['active_pages'] = 0
        c.execute('UPDATE collection_tasks SET ' + ','.join(k + '=?' for k in values) + ',updated_at=? WHERE id=?', (*values.values(), app.now(), task_id))


def recover():
    """Restart never silently resumes collection or starts network traffic."""
    with app.LOCKS['live'], app.db() as c:
        c.execute("UPDATE collection_tasks SET status='interrupted',active_pages=0,detail='服务已重启；已入库记录保留，请手动新建一批采集',finished_at=?,updated_at=? WHERE finished_at IS NULL", (app.now(), app.now()))


def start(body, mode='live', *, resume_from=None, lookback_hours=None, include_keywords='', exclude_keywords='', recovery_since=None, recovery_plan=None):
    if mode != 'live':
        raise ValueError('演示区不访问抖音；请切换正式数据')
    kind, target, videos, comments, interactive, request_id = options(body)
    transport = transport_option(body)
    concurrency = min(page_options(body), videos)
    if lookback_hours is None:
        lookback_hours = body.get('lookback_hours')
    include_keywords = comment_filters.normalize(include_keywords, '评论关键词')
    exclude_keywords = comment_filters.normalize(exclude_keywords, '屏蔽词')
    if lookback_hours is not None and (type(lookback_hours) is not int or not 1 <= lookback_hours <= 8760):
        raise ValueError('评论时间范围须为 1–8760 小时的整数')
    with GUARD, app.LOCKS['live'], app.db() as c:
        pending = []
        comment_since = app.timestamp(recovery_since) if recovery_since is not None else None
        if resume_from is not None:
            parent = c.execute('SELECT * FROM collection_tasks WHERE id=?', (int(resume_from),)).fetchone()
            if not parent or not parent['finished_at'] or int(resume_from) in ACTIVE:
                raise ValueError('原会话仍在运行，或任务不存在')
            pending = c.execute("SELECT * FROM collection_checkpoints WHERE task_id=? AND status NOT IN ('done','unavailable') ORDER BY rowid", (parent['id'],)).fetchall()
            if not pending:
                raise ValueError('没有可继续的视频断点；可新建一批关键词搜索')
            kind, target, videos, comments, interactive = parent['kind'], parent['target'], len(pending), parent['comment_limit'], parent['interactive']
            concurrency = min(parent['page_concurrency'], videos)
            lookback_hours, comment_since = parent['lookback_hours'], parent['comment_since']
            include_keywords, exclude_keywords = parent['include_keywords'], parent['exclude_keywords']
            transport = parent['transport']
        old = c.execute('SELECT * FROM collection_tasks WHERE request_id=?', (request_id,)).fetchone()
        if old:
            old_parent = c.execute('SELECT parent_task_id FROM collection_resumes WHERE task_id=?', (old['id'],)).fetchone()
            if (old_parent[0] if old_parent else None) != resume_from:
                raise ValueError('同一请求 ID 的恢复来源不一致')
            if tuple(old[x] for x in ('kind', 'target', 'video_limit', 'comment_limit', 'interactive', 'page_concurrency')) != (kind, target, videos, comments, interactive, concurrency):
                raise ValueError('同一请求 ID 的采集参数不一致')
            if old['lookback_hours'] != lookback_hours:
                raise ValueError('同一请求 ID 的评论时间范围不一致')
            if recovery_since is not None and old['comment_since'] != comment_since:
                raise ValueError('同一请求 ID 的恢复时间起点不一致')
            if (old['include_keywords'], old['exclude_keywords']) != (include_keywords, exclude_keywords):
                raise ValueError('同一请求 ID 的评论过滤条件不一致')
            if old['transport'] != transport:
                raise ValueError('同一请求 ID 的采集通道不一致')
            return {'id': old['id'], 'status': old['status']}
        if ACTIVE:
            raise ValueError('已有采集会话，请先完成或停止它；视频读取并发在同一任务内配置')
        import login_recovery
        if login_recovery.busy():
            raise ValueError('账号正在恢复登录，请等待完成后再采集')
        source = c.execute("SELECT id FROM sources WHERE kind='browser' ORDER BY id LIMIT 1").fetchone()
        source_id = source[0] if source else c.execute("INSERT INTO sources(name,kind,status,notes) VALUES('抖音 · 本机浏览器','browser','unverified','本机普通浏览器页面读取；只记录实际加载数据，不代表平台授权或全网覆盖')").lastrowid
        t = app.now()
        if lookback_hours is not None and comment_since is None:
            comment_since = (datetime.fromisoformat(t) - timedelta(hours=lookback_hours)).isoformat(timespec='seconds')
        task_id = c.execute('INSERT INTO collection_tasks(request_id,kind,target,video_limit,comment_limit,interactive,status,detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (request_id, kind, target, videos, comments, interactive, 'queued', '等待后端 HTTP 读取' if transport == 'http' else '等待本机浏览器启动', t, t)).lastrowid
        c.execute('UPDATE collection_tasks SET transport=? WHERE id=?', (transport, task_id))
        c.execute('UPDATE collection_tasks SET page_concurrency=?,lookback_hours=?,comment_since=?,include_keywords=?,exclude_keywords=? WHERE id=?', (concurrency, lookback_hours, comment_since, include_keywords, exclude_keywords, task_id))
        if pending:
            c.execute('INSERT INTO collection_resumes VALUES(?,?)', (task_id, resume_from))
            for row in pending:
                c.execute('INSERT INTO collection_checkpoints(task_id,video_id,video_title,video_url,updated_at) VALUES(?,?,?,?,?)', (task_id, row['video_id'], row['video_title'], row['video_url'], t))
        if recovery_plan is not None:
            import collection_scheduler
            collection_scheduler.attach_login_continuation(c, recovery_plan, task_id)
        app.event(c, 'collector', f'创建自建采集任务 #{task_id}：{target}')
        control = {'process': None, 'cancel': False, 'source_id': source_id, 'cancel_at': None, 'timeout': False}
        ACTIVE[task_id] = control
    # Transaction must commit before the worker can read this task.
    thread = threading.Thread(target=run, args=(task_id, control), daemon=True, name=f'collector-{task_id}')
    thread.start()
    return {'id': task_id, 'status': 'queued'}


def resume(task_id, request_id, mode='live', *, recovery_plan=None):
    # Checkpoints are loaded from our DB, never accepted from the HTTP payload.
    return start({'kind': 'search', 'target': '断点恢复', 'request_id': request_id}, mode, resume_from=int(task_id), recovery_plan=recovery_plan)


def allowed_video_ids(connection, task):
    if connection.execute('SELECT 1 FROM collection_resumes WHERE task_id=?', (task['id'],)).fetchone():
        return {r[0] for r in connection.execute('SELECT video_id FROM collection_checkpoints WHERE task_id=?', (task['id'],))}
    if task['kind'] == 'video':
        return {row['video_id'] for row in video_targets(task['target'])}
    return None


def checkpoint(task_id, message):
    records = message.get('records')
    if message.get('type') == 'targets':
        if not isinstance(records, list) or not 1 <= len(records) <= 5:
            raise ValueError('视频断点范围无效')
        with app.LOCKS['live'], app.db() as c:
            task = c.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            if not task or task['finished_at']:
                raise ValueError('采集任务已经结束')
            existing = {r[0] for r in c.execute('SELECT video_id FROM collection_checkpoints WHERE task_id=?', (task_id,))}
            allowed = allowed_video_ids(c, task)
            prepared = []
            for row in records:
                video_id = str(row.get('video_id', ''))
                if not re.fullmatch(r'\d{5,30}', video_id):
                    raise ValueError('视频断点需要原始数字 ID')
                url = canonical_video(video_id)
                if allowed is not None and video_id not in allowed:
                    raise ValueError('断点超出指定视频范围')
                existing.add(video_id)
                prepared.append((task_id, video_id, app.clean(row.get('video_title')) or video_id, url, app.now()))
            if len(existing) > task['video_limit']:
                raise ValueError('断点超出本批视频上限')
            c.executemany('INSERT OR IGNORE INTO collection_checkpoints(task_id,video_id,video_title,video_url,updated_at) VALUES(?,?,?,?,?)', prepared)
            candidate_pool.mark_selected(c, task, records)
    elif message.get('type') == 'checkpoint':
        status = message.get('status')
        if status not in ('reading', 'done', 'partial', 'unavailable'):
            raise ValueError('断点状态无效')
        with app.LOCKS['live'], app.db() as c:
            row = c.execute('SELECT status FROM collection_checkpoints WHERE task_id=? AND video_id=?', (task_id, message.get('video_id'))).fetchone()
            if not row:
                raise ValueError('断点不存在')
            if row['status'] in ('done', 'unavailable') and status != row['status']:
                raise ValueError('已完成断点不能退回读取状态')
            c.execute('UPDATE collection_checkpoints SET status=?,detail=?,updated_at=? WHERE task_id=? AND video_id=?', (status, app.clean(message.get('detail'), 500), app.now(), task_id, message['video_id']))


def command(task_id, action, mode='live'):
    if mode != 'live' or action not in ('cancel', 'resume'):
        raise ValueError('采集操作无效')
    task_id = int(task_id)
    with GUARD:
        ctl = ACTIVE.get(task_id)
        if not ctl:
            raise ValueError('会话已关闭；请新建一批采集，重复评论会自动去重')
        with app.db() as c:
            task = c.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
        if action == 'resume' and task['status'] not in WAITING:
            raise ValueError('当前任务不是等待人工处理状态')
        if action == 'cancel':
            ctl['cancel'] = True
            ctl['cancel_at'] = time.monotonic()
        process = ctl['process']
        if process and process.poll() is None:
            try:
                process.stdin.write(json.dumps({'command': action}) + '\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        if action == 'cancel':
            update(task_id, status='cancelling', detail='正在停止采集任务；已入库数据保留')
    return {'id': task_id, 'action': action}


def observe(task_id, source_id, message):
    """Only called by our worker pipe, never exposed as an unauthenticated ingest API."""
    kind = message.get('type')
    row = message.get('record')
    if kind not in ('video', 'comment') or not isinstance(row, dict):
        raise ValueError('采集事件格式不正确')
    url = canonical_video(row.get('video_id'))
    external = str(row.get('comment_id') if kind == 'comment' else row['video_id'])
    if not re.fullmatch(r'\d{5,30}', external):
        raise ValueError('没有有效的原始内容 ID，不入库')
    with app.LOCKS['live'], app.db() as c:
        task = c.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
        if not task or task['finished_at']:
            raise ValueError('任务已结束')
        # A queued worker event may have passed its cancel check before shutdown.
        # Recheck under the same DB lock used by the stop operation.
        if task['status'] == 'cancelling':
            return
        allowed = allowed_video_ids(c, task)
        if allowed is not None and str(row['video_id']) not in allowed:
            raise ValueError('评论不属于本任务视频')
        existing = c.execute('SELECT 1 FROM collection_observations WHERE task_id=? AND kind=? AND external_id=?', (task_id, kind, external)).fetchone()
        if existing:
            return
        if kind == 'video':
            if task['videos'] >= task['video_limit']:
                raise ValueError('超过本批视频数量上限')
            previous_video = c.execute('SELECT id,title FROM videos WHERE source_id=? AND external_id=?', (source_id, row['video_id'])).fetchone()
            title = app.clean(row.get('video_title')) or row['video_id']
            c.execute("INSERT INTO videos(source_id,external_id,title,url,created_at) VALUES(?,?,?,?,?) ON CONFLICT(source_id,external_id) DO UPDATE SET title=CASE WHEN excluded.title!=excluded.external_id THEN excluded.title ELSE videos.title END", (source_id, row['video_id'], app.clean(row.get('video_title')) or row['video_id'], url, app.now()))
            import video_metadata
            video_pk = c.execute('SELECT id FROM videos WHERE source_id=? AND external_id=?',(source_id,row['video_id'])).fetchone()[0]
            video_metadata.save(c,video_pk,row.get('metrics'))
            if previous_video and title != row['video_id'] and previous_video['title'] != title:
                app.reset_video_rules(c, previous_video['id'])
            if title != row['video_id']:
                c.execute('UPDATE collection_checkpoints SET video_title=? WHERE task_id=? AND video_id=?', (title, task_id, row['video_id']))
            counts = {'videos': task['videos'] + 1}
        else:
            if not c.execute("SELECT 1 FROM collection_observations WHERE task_id=? AND kind='video' AND external_id=?", (task_id, row['video_id'])).fetchone():
                raise ValueError('评论没有对应的本批视频证据')
            n = c.execute("SELECT COUNT(*) FROM collection_observations WHERE task_id=? AND kind='comment' AND page_url=?", (task_id, url)).fetchone()[0]
            if n >= task['comment_limit']:
                raise ValueError('超过本视频评论数量上限')
            filtered = None
            if task['comment_since']:
                try:
                    published = app.timestamp(row.get('published_at') or row.get('create_time'))
                except ValueError:
                    published = None
                if published is None:
                    filtered = 'filtered_unknown'
                elif datetime.fromisoformat(published) < datetime.fromisoformat(task['comment_since']):
                    filtered = 'filtered_old'
                elif datetime.fromisoformat(published) > datetime.fromisoformat(app.now()):
                    filtered = 'filtered_future'
            if not filtered:
                filtered = comment_filters.rejection(row.get('text') or row.get('content'), task['include_keywords'], task['exclude_keywords'])
            if filtered:
                counts = {filtered: task[filtered] + 1}
            else:
                result = app.ingest({'source_id': source_id, 'records': [{**row, 'video_url': url}]}, allow_browser_source=True, connection=c)
                counts = {'comments': task['comments'] + 1, **{k: task[k] + result[k] for k in ('inserted', 'duplicate', 'revised')}}
        digest = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        published_at = None
        if kind == 'comment':
            try:
                published_at = app.timestamp(row.get('published_at') or row.get('create_time'))
            except ValueError:
                pass
        # Keep a minimal immutable display snapshot even when a comment is
        # filtered out. It is evidence, not a customer or a messaging task.
        disposition = next((k for k in ('inserted', 'revised', 'duplicate') if result[k]), '') if kind == 'comment' and not filtered else ''
        c.execute('INSERT INTO collection_observations(task_id,kind,external_id,page_url,observed_at,payload_hash,filter_reason,comment_text,published_at,nickname,user_identifier,ingest_disposition) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (task_id, kind, external, url, app.now(), digest, (filtered or '') if kind == 'comment' else '',
             app.clean(row.get('text') or row.get('content'), 5000) if kind == 'comment' else '', published_at,
             app.clean(row.get('nickname'), 120) if kind == 'comment' else '', app.clean(row.get('user_id'), 300) if kind == 'comment' else '', disposition))
        c.execute("UPDATE sources SET status='observed',last_received=? WHERE id=?", (app.now(), source_id))
        c.execute('UPDATE collection_tasks SET ' + ','.join(k+'=?' for k in counts) + ',page_url=?,updated_at=? WHERE id=?', (*counts.values(), url, app.now(), task_id))
    # Commit evidence before analysis; the model queue can work while the pipe
    # continues reading. Rejected records never trigger historical analysis.
    if kind == 'comment' and not filtered:
        analyze_observed(task_id, source_id)


def analyze_observed(task_id, source_id):
    """Initial screening is local; remote inference is only queued, never awaited."""
    try:
        with app.db() as c:
            ids = [r[0] for r in c.execute("""SELECT x.id FROM comments x
                JOIN collection_observations o ON o.external_id=x.external_id
                WHERE o.task_id=? AND o.kind='comment' AND o.filter_reason=''
                AND x.source_id=? AND x.analysis_method='pending' ORDER BY x.id LIMIT 1000""",
                (task_id, source_id))]
        if ids:
            app.analyze(comment_ids=ids)
    except Exception as exc:
        # Failure in screening must not turn a successful read into a failed
        # collection. The batch-final sweep retries local pending records only.
        with app.LOCKS['live'], app.db() as c:
            app.event(c, 'analysis', f'任务 #{task_id} 的逐条初筛未完成：{type(exc).__name__}；保留已入库记录')


def kill_worker(process):
    """Last resort, restricted to the still-live Popen child and its browser descendants."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    else:
        process.kill()


def parallel_progress(task_id, message):
    values = [message.get(k) for k in ('active_pages', 'peak_pages', 'page_concurrency')]
    if any(type(value) is not int for value in values):
        raise ValueError('并发进度字段无效')
    active, peak, limit = values
    with app.LOCKS['live'], app.db() as c:
        row = c.execute('SELECT page_concurrency,peak_pages,finished_at FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
        if not row or row['finished_at'] or limit != row['page_concurrency'] or not 0 <= active <= peak <= limit or peak < row['peak_pages']:
            raise ValueError('并发进度超出配置或任务已经结束')
        c.execute('UPDATE collection_tasks SET active_pages=?,peak_pages=?,updated_at=? WHERE id=?', (active, peak, app.now(), task_id))


def cached_video_metadata(c, task, source_id, resume_targets):
    """New fixed-video batches have no checkpoints yet; resolve their saved cache too."""
    import video_metadata
    targets = resume_targets or (video_targets(task['target']) if task['kind'] == 'video' else [])
    ids = list(dict.fromkeys(row['video_id'] for row in targets))
    if not ids:
        return {}, {}
    marks = ','.join('?' for _ in ids)
    rows = list(c.execute(f'SELECT id,external_id,title FROM videos WHERE source_id=? AND external_id IN ({marks})',
                          (source_id, *ids)))
    return ({r['external_id']: r['title'] for r in rows if r['title'] != r['external_id']},
            {r['external_id']: video_metadata.project(c, r['id']) for r in rows})


def run(task_id, control):
    process = None
    terminal_received = False
    try:
        with app.db() as c:
            task = dict(c.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone())
            resumed = c.execute('SELECT 1 FROM collection_resumes WHERE task_id=?', (task_id,)).fetchone()
            resume_targets = [dict(r) for r in c.execute('SELECT video_id,video_title,video_url FROM collection_checkpoints WHERE task_id=? ORDER BY rowid', (task_id,))] if resumed else []
            known_titles, known_metrics = cached_video_metadata(c, task, control['source_id'], resume_targets)
            candidate_policy = candidate_pool.configuration(c, task) if not resumed else None
        node, package = dependencies()
        if task['transport'] == 'http':
            import collector_http
            if not collector_http.python_path().is_file():
                update(task_id, status='dependency_missing', detail='运行 python scripts/setup-collection-http.py 安装可选 HTTP 依赖', finished_at=app.now())
                return
            command_line = [str(collector_http.python_path()), str(BASE / 'collector_http_worker.py')]
        else:
            if not node or not package.is_dir():
                update(task_id, status='dependency_missing', detail='缺少 Node 或 Playwright；运行 python manage.py doctor --require-browser 检查依赖', finished_at=app.now())
                return
            command_line = [node, str(BASE / 'collector_worker.cjs')]
        env = {**os.environ, 'CLUBOPS_PLAYWRIGHT': str(package), 'CLUBOPS_DATA_DIR': str(app.DATA_DIR), 'PYTHONIOENCODING': 'utf-8'}
        process = subprocess.Popen(command_line, cwd=BASE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding='utf-8', bufsize=1, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with GUARD:
            control['process'] = process
        config = {**task, 'profile_dir': str(app.DATA_DIR / 'browser-profile'), 'resume_targets': resume_targets,
                  'candidate_policy': candidate_policy,
                  'resolve_video_titles': True, 'known_video_titles': known_titles,
                  'refresh_video_metrics': True, 'known_video_metrics': known_metrics,
                  'captcha': captcha_runtime.configuration()}
        process.stdin.write(json.dumps(config, ensure_ascii=False) + '\n')
        process.stdin.flush()
        if control['cancel']:
            process.stdin.write('{"command":"cancel"}\n')
            process.stdin.flush()
        # Hard stop for hung child processes; normal cancellation closes Chromium gracefully.
        deadline = time.monotonic() + (210 if task['transport'] == 'http' else 920)
        def watchdog():
            while process.poll() is None:
                expired = time.monotonic() >= deadline
                if expired or control.get('cancel_at') and time.monotonic() - control['cancel_at'] >= 12:
                    control['timeout'] = expired
                    try:
                        kill_worker(process)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    return
                time.sleep(1)
        threading.Thread(target=watchdog, daemon=True).start()
        for line in iter(lambda: process.stdout.readline(100001), ''):
            if len(line) > 100_000:
                raise ValueError('采集输出过大')
            message = json.loads(line)
            typ = message.get('type')
            if typ == 'verification':
                record_verification(task_id, message.get('event'))
            elif typ == 'parallel':
                if not control['cancel']:
                    parallel_progress(task_id, message)
            elif typ in ('targets', 'checkpoint'):
                if not control['cancel']:
                    checkpoint(task_id, message)
            elif typ == 'candidates':
                if not control['cancel']:
                    with app.LOCKS['live'], app.db() as c:
                        current = c.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
                        candidate_pool.record(c, current, message.get('records'))
            elif typ in ('video', 'comment'):
                if not control['cancel']:
                    observe(task_id, control['source_id'], message)
            elif typ == 'status':
                status = message.get('status')
                if status not in WAITING | {'running'} | TERMINAL:
                    raise ValueError('采集状态无效')
                values = {'status': 'cancelling' if control['cancel'] and status not in TERMINAL else status, 'detail': app.clean(message.get('detail'), 700)}
                if status in TERMINAL:
                    terminal_received = True
                    values['finished_at'] = app.now()
                update(task_id, **values)
            elif typ == 'skipped':
                with app.db() as c:
                    c.execute('UPDATE collection_tasks SET skipped=skipped+? WHERE id=?', (min(500, max(0, int(message.get('count', 1)))), task_id))
            elif typ == 'diagnostic':
                snapshot = message.get('snapshot', {})
                allowed = {'title', 'page_url', 'visible_text', 'video_links', 'responses', 'navigation_error', 'navigation_http_status', 'navigation_retry_after_seconds', 'processing'}
                snapshot = {k: v for k, v in snapshot.items() if k in allowed}
                encoded = json.dumps(snapshot, ensure_ascii=False)
                if len(encoded) <= 20000:
                    with app.db() as c:
                        n = c.execute('SELECT COUNT(*) FROM collection_diagnostics WHERE task_id=?', (task_id,)).fetchone()[0]
                        if n < 25:
                            c.execute('INSERT INTO collection_diagnostics(task_id,stage,snapshot,created_at) VALUES(?,?,?,?)', (task_id, app.clean(message.get('stage'), 80), encoded, app.now()))
        process.wait(timeout=10)
        if control.get('timeout'):
            terminal_received = True
            update(task_id, status='timeout', detail='采集进程超过时间上限，已终止本任务的工作进程；已入库数据保留', finished_at=app.now())
        if not terminal_received:
            with app.db() as c:
                previous = c.execute('SELECT status FROM collection_tasks WHERE id=?', (task_id,)).fetchone()[0]
            if previous in WAITING and not control['cancel']:
                update(task_id, finished_at=app.now())
            else:
                update(task_id, status='cancelled' if control['cancel'] else 'interrupted', detail='会话已结束，已入库记录保留；未确认完成', finished_at=app.now())
    except Exception as exc:
        # Do not persist raw browser exceptions, which can include request tokens or profile paths.
        detail = '采集器执行失败，已入库数据保留；请检查依赖或重试。错误类型：' + type(exc).__name__
        update(task_id, status='failed', detail=detail, finished_at=app.now())
    finally:
        if process:
            if process.poll() is None:
                try:
                    process.stdin.write('{"command":"cancel"}\n'); process.stdin.flush()
                    process.wait(timeout=12)
                except (OSError, subprocess.TimeoutExpired):
                    kill_worker(process)
            process.stdin.close()
            process.stdout.close()
        analyze_observed(task_id, control['source_id'])
        try:
            with app.LOCKS['live'], app.db() as c:
                current = c.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
                candidate_pool.settle(c, current)
        except Exception as error:
            with app.db() as c:
                app.event(c, 'candidate-pool', f'任务 #{task_id} 候选反馈未完成：{type(error).__name__}；已有采集结果保留')
        with GUARD:
            ACTIVE.pop(task_id, None)
        with app.db() as c:
            row = c.execute('SELECT status,comments FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            app.event(c, 'collector', f"采集任务 #{task_id} 结束：{row['status']}，观察到 {row['comments']} 条评论")
        import login_recovery
        login_recovery.after_collection(task_id)


def shutdown():
    with GUARD:
        ids = list(ACTIVE)
    for task_id in ids:
        try:
            command(task_id, 'cancel')
        except ValueError:
            pass
