"""One explicit, persistent monitor configuration. Reading state never starts work."""
import json
import re
from datetime import datetime

import clubops as app
import collector
import collection_scheduler as scheduler
import comment_filters


def integer(value, label, low, high):
    if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and value.isascii() and value.isdigit()):
        raise ValueError(f'{label}须为 {low}–{high} 的整数')
    value = int(value)
    if not low <= value <= high:
        raise ValueError(f'{label}须为 {low}–{high} 的整数')
    return value


def freshness_target(c):
    row = c.execute("SELECT value FROM settings WHERE key='collection_freshness_target_seconds'").fetchone()
    return integer(json.loads(row[0]), '采集时效目标（秒）', 10, 3600) if row else 60


def state(mode='live'):
    with collector.GUARD, app.LOCKS[mode], app.db(mode) as c:
        row = c.execute('SELECT * FROM collection_plans WHERE continuous=1').fetchone()
        config = c.execute("SELECT value FROM settings WHERE key='keywords'").fetchone()
        keyword = re.split(r'[,，\n]', json.loads(config[0]) if config else '')[0].strip()
        result = dict(row) if row else dict(id=None, kind='search', target=keyword or '无畏契约陪玩',
            lookback_hours=1, interval_seconds=30, video_limit=3, comment_limit=30,
            page_concurrency=1, include_keywords='', exclude_keywords='', status='paused', detail='监控未开启；默认只接收最近 1 小时发布的评论',
            next_run_at=None, run_count=0, settled_count=0, last_task_id=None)
        task_id = result['last_task_id']
        import collection_accounts
        result['collection_account']=collection_accounts.binding(c,task_id) if task_id else None
        control = collector.ACTIVE.get(task_id) if mode == 'live' else None
        recovery = c.execute('''SELECT r.task_id FROM collection_manual_recoveries r
            JOIN collection_tasks t ON t.id=r.task_id
            WHERE r.parent_task_id=? AND r.settled_at IS NULL AND t.finished_at IS NULL
            ORDER BY r.task_id DESC LIMIT 1''', (task_id,)).fetchone() if mode == 'live' else None
        recovery_id = recovery['task_id'] if recovery and recovery['task_id'] in collector.ACTIVE else None
        if recovery_id:
            control = collector.ACTIVE[recovery_id]
        result.update(enabled=result['status'] == 'running', active_task_id=task_id if control else None,
            freshness_target_seconds=freshness_target(c),
            stopping=bool(control and control['cancel']), transport=result.get('transport', 'local_browser'))
        result['verification_recovery'] = recovery_id
        if recovery_id:
            result['active_task_id'] = recovery_id
        if task_id:
            row = c.execute('SELECT status,comments,comment_since,filtered_old,filtered_unknown,filtered_future,filtered_keyword,filtered_blocked FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
            result['last_batch'] = dict(row) if row else None
        else:
            result['last_batch'] = None
        return result


def save(body, mode='live'):
    if mode != 'live':
        raise ValueError('监控仅可在正式工作区配置')
    with collector.GUARD, app.LOCKS['live']:
        previous = state()
        if previous['enabled'] or previous['active_task_id']:
            raise ValueError('请先关闭监控并等待当前任务停止，再修改配置')
        merged = {**previous, **body}
        transport = collector.transport_option(merged)
        hours = integer(merged['lookback_hours'], '评论时间范围（小时）', 1, 8760)
        interval = integer(merged['interval_seconds'], '监控间隔（秒）', 30, 86400)
        target_seconds = integer(merged['freshness_target_seconds'], '采集时效目标（秒）', 10, 3600)
        videos = integer(merged['video_limit'], '每批视频数', 1, 5)
        comments = integer(merged['comment_limit'], '每视频观察评论数', 1, 100)
        concurrency = integer(merged['page_concurrency'], '视频并行数', 1, collector.MAX_PAGE_CONCURRENCY)
        included = comment_filters.normalize(merged['include_keywords'], '评论关键词')
        excluded = comment_filters.normalize(merged['exclude_keywords'], '屏蔽词')
        kind, target, videos, comments, _, _ = collector.options({**merged, 'video_limit': videos,
            'comment_limit': comments, 'interactive': True, 'request_id': 'monitor-validation'})
        values = ('评论监控', kind, target, videos, comments, interval, min(concurrency, videos), hours, included, excluded)
        with app.db() as c:
            c.execute("INSERT INTO settings(key,value) VALUES('collection_freshness_target_seconds',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(target_seconds),))
            if previous['id']:
                # Keep past counters, task links and evidence; only future batches use the new configuration.
                c.execute("UPDATE collection_plans SET name=?,kind=?,target=?,video_limit=?,comment_limit=?,interval_seconds=?,page_concurrency=?,lookback_hours=?,include_keywords=?,exclude_keywords=?,status='paused',next_run_at=NULL,detail='配置已保存，监控保持关闭',updated_at=? WHERE id=?", (*values, app.now(), previous['id']))
            else:
                c.execute("INSERT INTO collection_plans(name,kind,target,video_limit,comment_limit,interval_seconds,page_concurrency,lookback_hours,include_keywords,exclude_keywords,priority,run_limit,continuous,status,detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,2,1,1,'paused','配置已保存，监控保持关闭',?,?)", (*values, app.now(), app.now()))
            app.event(c, 'monitor', f'保存评论监控：最近 {hours} 小时；未启动采集')
            c.execute('UPDATE collection_plans SET transport=?,intent_version=intent_version+1 WHERE continuous=1', (transport,))
        return state()


def command(action, mode='live'):
    if mode != 'live' or action not in ('start', 'stop'):
        raise ValueError('监控操作无效')
    # Shared dispatch lock: once stop returns, no new batch can sneak through.
    with collector.GUARD, app.LOCKS['live']:
        current = state()
        if not current['id']:
            if action == 'stop':
                return current
            current = save({})
        if action == 'start':
            scheduler.command(current['id'], 'start', allow_monitor=True)
        else:
            scheduler.command(current['id'], 'pause', allow_monitor=True)
            task_id = current['active_task_id']
            if task_id:
                collector.command(task_id, 'cancel')
        return state()


def observation_analysis(c, comment_id, text, model_engine, *, summary=False):
    """Read the same current analysis as demand details, bound to the visible text."""
    import analysis_store
    empty = dict(category=None, analysis_method=None, analysis_state='missing',
                 analysis_reason=None, analysis_evidence=[])
    if not comment_id:
        return empty
    source, archive = analysis_store.inputs(c, 'comment', comment_id)
    if text != source['text']:
        return {**empty, 'analysis_state': 'snapshot_changed'}
    projected = dict(archive, video_title=source['title'],
                     parent_context={'status': 'available', 'raw_text': source['parent']})
    analysis_store.project(c, projected, model_engine=model_engine,details=not summary)
    method = projected['analysis_method']
    if summary:return dict(category=projected['category'],analysis_method=method,game=projected.get('game',''))
    state = method
    if method not in ('human', 'pending', 'model') and model_engine:
        model = projected.get('model_result')
        if model and model['engine'] == model_engine:
            state = model['status'] if model['status'] != 'completed' else method
        else:
            job = c.execute("""SELECT status FROM semantic_jobs WHERE evidence_type='comment'
                AND record_id=? AND input_hash=? AND engine=?""",
                (comment_id, projected['analysis_input_hash'], model_engine)).fetchone()
            if job and job['status'] != 'completed':
                state = job['status']
    rule = projected.get('rule_result') or {}
    model = projected.get('model_result') or {}
    job = c.execute("""SELECT created_at,started_at,finished_at,status FROM semantic_jobs
        WHERE evidence_type='comment' AND record_id=? AND input_hash=? AND engine=?""",
        (comment_id, projected['analysis_input_hash'], model_engine)).fetchone() if model_engine else None
    current_model = model if analysis_store.compatible_engine(model.get('engine'),model_engine) else {}
    relevance=projected.get('companion_relevance')
    if method=='rules' and state=='rules' and not projected['model_routing']['model_allowed']:
        state='skipped'
    # Keep explanation and conclusion from the same current, input-bound result.
    # Only classification quotes from this comment explain its intent; title and
    # parent quotes may describe context but must not imply the commenter needs it.
    evidence = (projected.get('facts') or {}).get('evidence', []) if method == 'model' else []
    category_quotes = [e for e in evidence if isinstance(e, dict)
        and e.get('kind') == 'category' and e.get('source') == 'comment'
        and isinstance(e.get('text'), str) and e['text'] and e['text'] in text]
    return dict(category=projected['category'], game=projected.get('game',''),author_profile=projected.get('author_profile'),author_role=projected.get('author_role'), analysis_method=method, analysis_state=state,
        analysis_reason=projected.get('reason') if method in ('model', 'rules', 'human') else None,
        analysis_evidence=category_quotes, companion_relevance=relevance,model_routing=projected['model_routing'],
        rule_finished_at=rule.get('finished_at'),
        model_queued_at=job['created_at'] if job else None,
        model_started_at=current_model.get('started_at'),
        model_finished_at=current_model.get('finished_at') if method == 'model' else None)


def elapsed_seconds(start, end):
    """Missing, malformed, naive or reversed timestamps are unknown, never zero."""
    try:
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if a.tzinfo is None or b.tzinfo is None:
            return None
        seconds = (b-a).total_seconds()
        return round(seconds, 3) if seconds >= 0 else None
    except (ValueError, TypeError):
        return None


def timing(row, target_seconds=60):
    collection = elapsed_seconds(row.get('published_at'), row.get('first_seen_at'))
    end_to_end = elapsed_seconds(row.get('published_at'), row.get('model_finished_at'))
    # A completed model result alone cannot validate a malformed source clock.
    if collection is None or elapsed_seconds(row.get('first_seen_at'), row.get('model_finished_at')) is None:
        end_to_end = None
    return dict(collection_seconds=collection,
        rule_seconds=elapsed_seconds(row.get('first_seen_at'), row.get('rule_finished_at')),
        queue_seconds=elapsed_seconds(row.get('model_queued_at'), row.get('model_started_at')),
        model_seconds=elapsed_seconds(row.get('model_started_at'), row.get('model_finished_at')),
        end_to_end_seconds=end_to_end,
        fresh_at_collection=collection <= 3600 if collection is not None else None,
        within_target=collection <= target_seconds if collection is not None else None)


def timeliness(rows, target_seconds=60):
    # Re-observing an old record must not inflate this batch's speed sample.
    candidates = [r for r in rows if not r['filter_reason'] and r.get('ingest_disposition') == 'inserted']
    values = sorted(r['timing']['collection_seconds'] for r in candidates
                    if r['timing']['collection_seconds'] is not None)
    return dict(target_seconds=target_seconds, metric='publication_to_collection', new_comments=len(candidates), measured=len(values),
        within_target=sum(v <= target_seconds for v in values), pending_or_unknown=len(candidates)-len(values),
        p50_seconds=values[(len(values)-1)//2] if values else None,
        p95_seconds=values[max(0, (95*len(values)+99)//100-1)] if values else None,
        scope='new_accepted_comments_in_latest_batch')


def results(mode='live'):
    """Latest batch observations, including rejection evidence, at most 500 rows."""
    import semantic
    model_engine = semantic.state()['engine'] if mode == 'live' else None
    with app.LOCKS[mode], app.db(mode) as c:
        target_seconds = freshness_target(c)
        task = c.execute('SELECT * FROM collection_tasks ORDER BY id DESC LIMIT 1').fetchone()
        if not task:
            return {'task': None, 'rows': [], 'counts': {'observed': 0, 'accepted': 0, 'filtered': 0}}
        source = c.execute("SELECT id FROM sources WHERE kind='browser' ORDER BY id LIMIT 1").fetchone()
        observed = c.execute("""SELECT o.*,x.id AS comment_id,x.raw_text AS archive_text,
            x.published_at AS archive_published,x.observed_nickname AS archive_nickname,x.discovered_at AS first_seen_at,
            x.analysis_method,x.category,p.external_id AS archive_user,v.title AS video_title
            FROM collection_observations o
            LEFT JOIN comments x ON x.source_id=? AND x.external_id=o.external_id
            LEFT JOIN people p ON p.id=x.person_id
            LEFT JOIN videos v ON v.source_id=? AND v.url=o.page_url
            WHERE o.task_id=? AND o.kind='comment'
            ORDER BY o.observed_at DESC,o.rowid DESC LIMIT 500""", (source[0] if source else -1, source[0] if source else -1, task['id'])).fetchall()
        rows = []
        for row in observed:
            snapshot = bool((row['comment_text'] or '').strip())
            text = row['comment_text'] if snapshot else row['archive_text'] or ''
            if not text.strip():
                continue
            accepted = not row['filter_reason']
            analysis = observation_analysis(c, row['comment_id'], text, model_engine) if accepted else {
                'category': None, 'analysis_method': None, 'analysis_state': None}
            rows.append({'external_id': row['external_id'], 'text': text,
                'text_origin': 'observation' if snapshot else 'archive',
                'nickname': row['nickname'] if snapshot else row['archive_nickname'] or '',
                'user_identifier': row['user_identifier'] if snapshot else row['archive_user'] or '',
                'published_at': row['published_at'] if snapshot else row['archive_published'],
                'observed_at': row['observed_at'], 'first_seen_at': row['first_seen_at'],
                'video_url': row['page_url'], 'video_title': row['video_title'] or '',
                'filter_reason': row['filter_reason'], 'comment_id': row['comment_id'],
                'ingest_disposition': row['ingest_disposition'],
                **analysis,
                'include_matches': comment_filters.matches(text, task['include_keywords']) if snapshot else [],
                'exclude_matches': comment_filters.matches(text, task['exclude_keywords']) if snapshot else []})
            rows[-1]['timing'] = timing(rows[-1], target_seconds)
        accepted = sum(not r['filter_reason'] for r in rows)
        return {'task': {'id': task['id'], 'status': task['status'], 'target': task['target'],
                'comment_since': task['comment_since'], 'include_keywords': task['include_keywords'], 'exclude_keywords': task['exclude_keywords']},
            'rows': rows, 'counts': {'observed': len(rows), 'accepted': accepted, 'filtered': len(rows)-accepted},
            'timeliness': timeliness(rows, target_seconds),
            'coverage': dict(scope='observed_batch_only', all_douyin=False,
                observed_videos=task['videos'], video_limit=task['video_limit'],
                comment_limit_per_video=task['comment_limit'], latest_first_verified=False)}
