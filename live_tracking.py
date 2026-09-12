"""Explicit live tracking: frozen batches, optional durable room-library rotation."""
import json
import threading
from datetime import datetime, timedelta, timezone

import clubops as app
import live_monitor as live
import live_room_pool as pool

STOP = threading.Event()
THREAD = None
SCHEMA = '''
CREATE TABLE IF NOT EXISTS live_tracks (
 id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, config TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL, last_session_id INTEGER REFERENCES live_sessions(id),
 run_count INTEGER NOT NULL DEFAULT 0, next_run_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_live_track_active ON live_tracks((1)) WHERE status='enabled';
'''


def project(row):
    if not row:
        return dict(id=None, enabled=False, status='paused', run_count=0,
                    last_session_id=None, next_run_at=None, detail='持续跟踪未开启')
    value = dict(row)
    value['config'] = json.loads(value['config'])
    value['enabled'] = value['status'] == 'enabled'
    return value


def state(mode='live'):
    with live.GUARD, app.db(mode) as c:
        return project(c.execute('SELECT * FROM live_tracks ORDER BY id DESC LIMIT 1').fetchone())


def start(body, mode='live'):
    if mode != 'live':
        raise ValueError('演示区不启动真实直播跟踪')
    request_id = body.get('request_id')
    scope = body.get('scope', 'single')
    if scope not in ('single', 'library'):
        raise ValueError('直播跟踪范围无效')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('缺少持续跟踪请求 ID')
    with live.GUARD, app.LOCKS[mode], app.db(mode) as c:
        old = c.execute('SELECT * FROM live_tracks WHERE request_id=?', (request_id,)).fetchone()
        if old:
            return project(old)
        if live.ACTIVE or c.execute("SELECT 1 FROM live_tracks WHERE status='enabled'").fetchone():
            raise ValueError('已有直播会话或持续跟踪；请先停止')
        config = live.options(live.settings())
        # Tracking always runs in the background; a manual one-shot may show UI.
        config['interactive'] = False
        if scope == 'library':
            config['scope'] = 'library'
            pool.seed(c, config)
        stamp = app.now()
        track_id = c.execute('INSERT INTO live_tracks(request_id,config,status,detail,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            (request_id, json.dumps(config, ensure_ascii=False), 'enabled',
             '持续跟踪已开启，等待后台读取指定房间', stamp, stamp, stamp)).lastrowid
        if scope == 'library':
            c.execute('INSERT INTO live_pool_checks(track_id,next_discovery_at) VALUES(?,?)', (track_id, stamp))
            c.execute('UPDATE live_tracks SET detail=? WHERE id=?', ('直播间库持续监控已开启，等待串行读取已关注房间', track_id))
        return project(c.execute('SELECT * FROM live_tracks WHERE id=?', (track_id,)).fetchone())


def stop(track_id, mode='live'):
    if mode != 'live' or type(track_id) is not int:
        raise ValueError('持续跟踪停止参数无效')
    with live.GUARD:
        with app.LOCKS[mode], app.db(mode) as c:
            row = c.execute('SELECT * FROM live_tracks WHERE id=?', (track_id,)).fetchone()
            if not row:
                raise ValueError('持续跟踪记录不存在')
            c.execute("UPDATE live_tracks SET status='paused',next_run_at=NULL,detail=?,updated_at=? WHERE id=?",
                      ('已关闭持续跟踪，停止当前批次并阻止后续读取', app.now(), track_id))
        if row['last_session_id'] in live.ACTIVE:
            live.stop(row['last_session_id'])
    return state(mode)


def tick():
    """One admission under the live lock; status reads never call this function."""
    with live.GUARD:
        if STOP.is_set():
            return
        with app.LOCKS['live'], app.db() as c:
            row = c.execute("SELECT * FROM live_tracks WHERE status='enabled'").fetchone()
            if not row or live.ACTIVE:
                return
            row = dict(row)
            config = json.loads(row['config'])
            library = config.get('scope') == 'library'
            if library:
                pool.request_discovery(row['id'], config)
            if row['last_session_id'] and row['next_run_at'] is None:
                session = c.execute('SELECT * FROM live_sessions WHERE id=?', (row['last_session_id'],)).fetchone()
                if not session or not session['finished_at']:
                    c.execute("UPDATE live_tracks SET status='attention',detail=?,updated_at=? WHERE id=?",
                              ('上一批未正常结算，跟踪已暂停；请检查会话记录', app.now(), row['id']))
                    return
                # Only a healthy completed batch schedules a new connection.
                # Valid non-chat events can complete a quiet-room batch. Empty
                # responses cannot prove a healthy stream and still pause.
                if library and session['status'] in pool.ROTATABLE:
                    pool.settle(c, session, config)
                elif session['status'] != 'completed':
                    if library:
                        pool.record_problem(c, session)
                    status = 'ended' if session['status'] == 'ended' else 'attention'
                    c.execute('UPDATE live_tracks SET status=?,detail=?,updated_at=? WHERE id=?',
                              (status, '持续跟踪已停止：' + session['detail'], app.now(), row['id']))
                    return
                due = (datetime.fromisoformat(session['finished_at']) + timedelta(seconds=config['interval_seconds'])).isoformat(timespec='seconds')
                c.execute('UPDATE live_tracks SET next_run_at=?,detail=?,updated_at=? WHERE id=?',
                          (due, '本批已完成，按已保存间隔等待下一批；批次之间存在观察缺口', app.now(), row['id']))
                row['next_run_at'] = due
            if not row['next_run_at'] or datetime.now(timezone.utc) < datetime.fromisoformat(row['next_run_at']):
                return
            if library:
                selected = pool.select_room(c)
                if not selected:
                    next_room = c.execute('SELECT MIN(next_check_at) FROM live_rooms WHERE enabled=1').fetchone()[0]
                    due = max(pool.later(app.now(), 1), next_room) if next_room else pool.later(app.now(), 60)
                    c.execute('UPDATE live_tracks SET next_run_at=?,detail=?,updated_at=? WHERE id=?',
                              (due, '已关注房间正在等待复查；直播发现按计划继续', app.now(), row['id']))
                    return
                config = {**config, 'room_url': selected['room_url']}
        # start commits before its worker can consume data. GUARD serializes stop.
        try:
            result = live.start({'request_id': f"track-{row['id']}-batch-{row['run_count'] + 1}"},
                                _tracking_config=config)
        except Exception:
            with app.LOCKS['live'], app.db() as c:
                c.execute("UPDATE live_tracks SET status='attention',next_run_at=NULL,detail=?,updated_at=? WHERE id=?",
                          ('后台直播启动失败，跟踪已暂停；未自动重试', app.now(), row['id']))
            return
        with app.LOCKS['live'], app.db() as c:
            c.execute('UPDATE live_tracks SET last_session_id=?,run_count=run_count+1,next_run_at=NULL,detail=?,updated_at=? WHERE id=?',
                      (result['id'], '正在串行读取直播间库；下播和暂时无数据的房间按间隔复查' if library else '正在后台跟踪指定直播间；参数固定为开启时的配置', app.now(), row['id']))
            if library:
                c.execute('UPDATE live_rooms SET last_session_id=?,last_status=(SELECT status FROM live_sessions WHERE id=?) WHERE room_url=?',
                          (result['id'], result['id'], config['room_url']))


def recover():
    with live.GUARD, app.LOCKS['live'], app.db() as c:
        for session in c.execute('SELECT s.* FROM live_rooms r JOIN live_sessions s ON s.id=r.last_session_id '
                                 'WHERE s.finished_at IS NOT NULL AND COALESCE(r.last_settled_session_id,0)<s.id').fetchall():
            pool.settle(c, session, {**live.DEFAULTS, **json.loads(session['config'])})
        c.execute("UPDATE live_tracks SET status='paused',next_run_at=NULL,detail=?,updated_at=? WHERE status='enabled'",
                  ('服务重启，持续跟踪已关闭；已有会话和弹幕保留', app.now()))


def start_service():
    global THREAD
    if THREAD and THREAD.is_alive():
        return
    STOP.clear()
    def run():
        while not STOP.wait(1):
            try:
                tick()
            except Exception:
                # Do not loop a failed admission; preserve data for inspection.
                with live.GUARD, app.LOCKS['live'], app.db() as c:
                    c.execute("UPDATE live_tracks SET status='attention',next_run_at=NULL,detail=?,updated_at=? WHERE status='enabled'",
                              ('直播调度异常，已暂停；请检查后重新开启', app.now()))
    THREAD = threading.Thread(target=run, daemon=True, name='live-tracking')
    THREAD.start()


def shutdown():
    STOP.set()
    if THREAD:
        THREAD.join(timeout=3)
    recover()
