"""Bounded, durable model work after rule analysis. No platform traffic."""
import json
import threading
import time

import clubops as app
import analysis_store as store
import semantic
import asset_verticality

CAPACITY = 200
STOP = threading.Event()
WAKE = threading.Event()
ACTIVE = {}
THREAD = None
THREADS = []
MAX_WORKERS = 4
SCHEMA = '''
CREATE TABLE IF NOT EXISTS semantic_jobs (
 id INTEGER PRIMARY KEY, evidence_type TEXT NOT NULL, record_id INTEGER NOT NULL,
 input_hash TEXT NOT NULL, engine TEXT NOT NULL, config_json TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', result_id INTEGER REFERENCES intent_results(id),
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 UNIQUE(evidence_type,record_id,input_hash,engine)
);
CREATE INDEX IF NOT EXISTS idx_semantic_jobs_status ON semantic_jobs(status,id);
'''


def human_reviewed(c, kind, row):
    if row['analysis_method'] == 'human':
        return True
    return kind == 'live' and c.execute('SELECT 1 FROM live_judgments WHERE message_id=?', (row['id'],)).fetchone() is not None


def enqueue(kind, record_ids, mode='live'):
    summary = dict(queued=0, existing=0, skipped=0, full=0)
    if mode != 'live':
        return summary
    if kind not in ('comment', 'live') or not isinstance(record_ids, list) or len(record_ids) > 1000:
        raise ValueError('模型入队范围无效')
    if any(type(i) is not int or i <= 0 for i in record_ids):
        raise ValueError('模型入队记录无效')
    with app.LOCKS[mode], app.db(mode) as c:
        settings, issues = semantic.config()
        channel = semantic.state()
        if issues or not channel['can_analyze'] or not settings['auto_analyze'] or kind == 'live' and not settings['live_model_enabled']:
            return summary
        pending = c.execute("SELECT COUNT(*) FROM semantic_jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0]
        for record_id in dict.fromkeys(record_ids):
            source, row = store.inputs(c, kind, record_id)
            if row['analysis_method'] != 'rules' or human_reviewed(c, kind, row):
                summary['skipped'] += 1
                continue
            if not asset_verticality.routing(c,kind,record_id,refresh_asset=True)['model_allowed']:
                summary['skipped'] += 1
                continue
            fingerprint = store.digest(source)
            existing = c.execute('SELECT 1 FROM semantic_jobs WHERE evidence_type=? AND record_id=? AND input_hash=? AND engine=?',
                                 (kind, record_id, fingerprint, channel['engine'])).fetchone()
            model = store.latest(c, kind, record_id, 'model', fingerprint)
            if existing or model and model['engine'] == channel['engine']:
                summary['existing'] += 1
                continue
            if pending >= CAPACITY:
                summary['full'] += 1
                continue
            c.execute('''INSERT INTO semantic_jobs(evidence_type,record_id,input_hash,engine,config_json,status,created_at)
                VALUES(?,?,?,?,?,'queued',?)''', (kind, record_id, fingerprint, channel['engine'], json.dumps(settings, sort_keys=True), app.now()))
            pending += 1
            summary['queued'] += 1
        if summary['queued'] or summary['full']:
            app.event(c, 'model_queue', f"模型队列新增 {summary['queued']} 条；容量不足 {summary['full']} 条保留规则结果")
    WAKE.set()
    return summary


def state(mode='live'):
    with app.db(mode) as c:
        counts = {r['status']: r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM semantic_jobs GROUP BY status')}
        rows = [dict(r) for r in c.execute('''SELECT id,evidence_type,record_id,status,detail,result_id,created_at,started_at,finished_at
            FROM semantic_jobs ORDER BY id DESC LIMIT 20''')]
    settings, _ = semantic.config()
    return dict(capacity=CAPACITY, counts=counts, rows=rows, concurrency_limit=semantic.concurrency(settings),
                active=counts.get('queued', 0)+counts.get('running', 0)+counts.get('cancelling', 0),
                worker_running=any(t.is_alive() for t in THREADS))


def finish(c, job_id, result):
    c.execute('UPDATE semantic_jobs SET status=?,detail=?,result_id=?,finished_at=? WHERE id=?',
              (result['status'], result['detail'], result.get('id'), app.now(), job_id))


def cancel_all(mode='live', *, detail='用户停止自动分析；保留规则结果', evidence_type=None):
    if mode != 'live':
        raise ValueError('演示区没有自动模型任务')
    if evidence_type not in (None, 'comment', 'live'):
        raise ValueError('模型取消范围无效')
    with app.LOCKS[mode], app.db(mode) as c:
        scope = ' AND evidence_type=?' if evidence_type else ''
        params = (evidence_type,) if evidence_type else ()
        queued = c.execute("UPDATE semantic_jobs SET status='cancelled',detail=?,finished_at=? WHERE status='queued'" + scope, (detail, app.now()) + params).rowcount
        # Final model result and job outcome commit together under this lock.
        # A cancellation arriving after completion cannot relabel that result.
        running = [r[0] for r in c.execute("SELECT id FROM semantic_jobs WHERE status IN ('running','cancelling')" + scope, params)]
        for job_id in running:
            if job_id in ACTIVE:
                ACTIVE[job_id].set()
            c.execute("UPDATE semantic_jobs SET status='cancelling',detail=? WHERE id=?", (detail, job_id))
        if queued or running:
            app.event(c, 'model_queue', f'停止模型队列：取消等待 {queued} 条，正在结束 {len(running)} 条')
    WAKE.set()
    return dict(cancelled=queued, cancelling=len(running))


def run_one(*, adapter_factory=None):
    job_id = None
    try:
        if STOP.is_set():
            return False
        with app.LOCKS['live'], app.db() as c:
            settings, issues = semantic.config()
            limit = semantic.concurrency(settings)
            if STOP.is_set() or len(ACTIVE) >= limit or semantic.GUARD.full(limit):
                return False
            # New comments and newly observed live messages precede historical work.
            # Observation time prioritizes work; it is not a claimed publication time.
            row = c.execute("""SELECT j.* FROM semantic_jobs j
                LEFT JOIN comments x ON j.evidence_type='comment' AND x.id=j.record_id
                LEFT JOIN live_messages m ON j.evidence_type='live' AND m.id=j.record_id
                WHERE j.status='queued'
                AND NOT EXISTS (SELECT 1 FROM semantic_jobs running
                    WHERE running.status IN ('running','cancelling')
                    AND running.evidence_type=j.evidence_type AND running.record_id=j.record_id)
                ORDER BY CASE WHEN julianday(x.published_at) BETWEEN julianday(?)-1.0/24
                    AND julianday(?) OR julianday(m.observed_at) BETWEEN julianday(?)-1.0/24
                    AND julianday(?) THEN 0 ELSE 1 END,j.id LIMIT 1""", (app.now(),)*4).fetchone()
            if not row:
                return False
            job = dict(row)
            job_id = job['id']
            source, evidence = store.inputs(c, job['evidence_type'], job['record_id'])
            if job['evidence_type'] == 'live' and not settings['live_model_enabled']:
                finish(c, job_id, dict(status='cancelled', detail='直播模型分析已关闭；保留规则结果'))
                return True
            if (issues or semantic.effective_config(settings, job['evidence_type']) != semantic.effective_config(json.loads(job['config_json']), job['evidence_type']) or not settings['enabled'] or not settings['auto_analyze']
                    or semantic.state()['engine'] != job['engine'] or store.digest(source) != job['input_hash']):
                finish(c, job_id, dict(status='stale', detail='原文、上下文或模型配置已变化；未调用模型'))
                return True
            if evidence['analysis_method'] != 'rules' or human_reviewed(c, job['evidence_type'], evidence):
                finish(c, job_id, dict(status='skipped', detail='该记录已人工处理或不再需要自动分析'))
                return True
            route=asset_verticality.routing(c,job['evidence_type'],job['record_id'],refresh_asset=True)
            if not route['model_allowed']:
                finish(c,job_id,dict(status='skipped',detail=route['reason']+' 未调用模型。'))
                return True
            existing = store.latest(c, job['evidence_type'], job['record_id'], 'model', job['input_hash'])
            if existing and existing['engine'] == job['engine']:
                finish(c, job_id, dict(status='skipped', detail='此版本原文已有模型分析记录，未重复调用', id=existing['id']))
                return True
            event = threading.Event()
            ACTIVE[job_id] = event
            c.execute("UPDATE semantic_jobs SET status='running',started_at=? WHERE id=?", (app.now(), job_id))
        request = dict(evidence_type=job['evidence_type'], id=job['record_id'], input_hash=job['input_hash'], request_id=f'queue-job-{job_id}')
        try:
            semantic.analyze_one(request, adapter_factory=adapter_factory, cancel_event=event, expected_config=settings,
                                 on_finish=lambda c, result: finish(c, job_id, result))
        except semantic.ModelBusy:
            with app.LOCKS['live'], app.db() as c:
                if event.is_set():
                    finish(c, job_id, dict(status='cancelled', detail='分析开始前已取消'))
                else:
                    c.execute("UPDATE semantic_jobs SET status='queued',started_at=NULL WHERE id=?", (job_id,))
            return False
        except Exception:
            with app.LOCKS['live'], app.db() as c:
                finish(c, job_id, dict(status='cancelled' if event.is_set() else 'failed',
                                      detail='自动分析未完成；保留规则结果，没有自动重试'))
        return True
    except Exception:
        if job_id is None:
            raise
        with app.LOCKS['live'], app.db() as c:
            finish(c, job_id, dict(status='failed', detail='记录不可用或任务处理异常；保留规则结果，没有自动重试'))
        return True
    finally:
        if job_id is not None:
            with app.LOCKS['live']:
                ACTIVE.pop(job_id, None)


def recover():
    with app.LOCKS['live'], app.db() as c:
        c.execute("UPDATE semantic_jobs SET status='interrupted',detail='服务中断；保留规则结果，没有自动重跑',finished_at=? WHERE status IN ('queued','running','cancelling')", (app.now(),))


def start_service():
    global THREAD, THREADS
    if any(t.is_alive() for t in THREADS):
        return
    STOP.clear()
    def work():
        while not STOP.is_set():
            WAKE.clear()
            try:
                advanced = run_one()
            except Exception:
                advanced = False
            if not advanced:
                WAKE.wait(.5)
    THREADS = [threading.Thread(target=work, name=f'model-queue-{i+1}', daemon=True) for i in range(MAX_WORKERS)]
    THREAD = THREADS[0]
    for worker in THREADS:
        worker.start()


def shutdown():
    STOP.set()
    cancel_all(detail='服务正常关闭；停止等待和在途模型分析')
    WAKE.set()
    deadline = time.monotonic() + 5
    for worker in THREADS:
        if worker.is_alive():
            worker.join(timeout=max(0, deadline-time.monotonic()))
