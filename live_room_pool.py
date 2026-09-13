"""Durable room catalog and bounded, serial rotation for ongoing live monitoring."""
import json
import threading
from datetime import datetime, timedelta, timezone

import clubops as app
import live_monitor as live
from game_scope import exclusion_reason

SCHEMA = '''
CREATE TABLE IF NOT EXISTS live_rooms (
 room_url TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 last_session_id INTEGER REFERENCES live_sessions(id), last_settled_session_id INTEGER, last_status TEXT,
 last_checked_at TEXT, next_check_at TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
 successful_batches INTEGER NOT NULL DEFAULT 0, saved_messages INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_live_rooms_due ON live_rooms(enabled,next_check_at);
CREATE TABLE IF NOT EXISTS live_pool_checks (
 track_id INTEGER PRIMARY KEY REFERENCES live_tracks(id), next_discovery_at TEXT NOT NULL,
 discovery_status TEXT NOT NULL DEFAULT 'waiting', discovery_detail TEXT NOT NULL DEFAULT '',
 last_discovery_at TEXT
);
'''
ROTATABLE = {'completed', 'ended', 'no_data', 'disconnected'}


def later(stamp, seconds):
    return (datetime.fromisoformat(stamp) + timedelta(seconds=seconds)).isoformat(timespec='seconds')


def ingest(c, rows, source, stamp):
    for row in rows:
        try:
            url = live.room_url(row.get('room_url'))
        except (ValueError, TypeError):
            continue
        title = str(row.get('title') or '')[:300]
        c.execute('INSERT INTO live_rooms(room_url,title,source,first_seen_at,last_seen_at,next_check_at) '
                  'VALUES(?,?,?,?,?,?) ON CONFLICT(room_url) DO UPDATE SET '
                  "title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE live_rooms.title END, "
                  "source=CASE WHEN excluded.source='valorant_category' THEN excluded.source ELSE live_rooms.source END, "
                  'last_seen_at=MAX(live_rooms.last_seen_at,excluded.last_seen_at)',
                  (url, title, source, stamp, stamp, app.now()))
        import asset_verticality
        if exclusion_reason(title):c.execute('UPDATE live_rooms SET enabled=0 WHERE room_url=?',(url,))
        import asset_keywords
        asset_keywords.observe(c,'live:'+url,title)
        asset_verticality.refresh(c,'live',url)


def seed(c, config):
    """Retain prior observations and approved room; no external reads at start."""
    stamp = app.now()
    for row in c.execute('SELECT room_url,MIN(started_at) AS first_at,MAX(id) AS latest, '
                         "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS successes, "
                         'SUM(inserted) AS messages FROM live_sessions GROUP BY room_url').fetchall():
        if c.execute('SELECT 1 FROM live_rooms WHERE room_url=?', (row['room_url'],)).fetchone():
            continue
        last = c.execute('SELECT * FROM live_sessions WHERE id=?', (row['latest'],)).fetchone()
        ingest(c, [{'room_url': row['room_url']}], 'previous_observation', row['first_at'])
        c.execute('UPDATE live_rooms SET last_session_id=?,last_settled_session_id=?,last_status=?,last_checked_at=?,last_seen_at=?, '
                  'next_check_at=?,successful_batches=?,saved_messages=? WHERE room_url=?',
                  (last['id'], last['id'], last['status'], last['finished_at'], last['started_at'],
                   later(stamp, -1) if row['messages'] else stamp, row['successes'], row['messages'], row['room_url']))
    ingest(c, [{'room_url': config['room_url']}], 'saved_room', stamp)
    discovery = c.execute("SELECT value FROM settings WHERE key='live_discovery'").fetchone()
    if discovery:
        value = json.loads(discovery[0])
        ingest(c, value.get('rows', []), 'valorant_category', value.get('fetched_at') or stamp)


def state(mode='live'):
    stamp = app.now()
    with live.GUARD, app.db(mode) as c:
        active_id = next(iter(live.ACTIVE), 0) if mode == 'live' else 0
        counts = dict(c.execute('SELECT COUNT(*) AS total,COALESCE(SUM(enabled),0) AS enabled, '
            'COALESCE(SUM(successful_batches>0),0) AS verified, '
            'COALESCE(SUM(enabled=1 AND next_check_at<=? AND COALESCE(last_session_id,-1)<>?),0) AS due, '
            'COALESCE(SUM(enabled=1 AND next_check_at>? AND COALESCE(last_session_id,-1)<>?),0) AS cooling FROM live_rooms',
            (stamp, active_id, stamp, active_id)).fetchone())
        rows = [dict(r) for r in c.execute('SELECT * FROM live_rooms ORDER BY enabled DESC,next_check_at,room_url LIMIT 100')]
        import asset_verticality
        verticality=asset_verticality.profiles(c,'live')
        for row in rows:row['verticality']=verticality.get(row['room_url'])
        counts['vertical']=sum(verticality.get(r[0],{}).get('matched',False) for r in c.execute('SELECT room_url FROM live_rooms'))
        discovery = c.execute('SELECT * FROM live_pool_checks ORDER BY track_id DESC LIMIT 1').fetchone()
    return {'counts': counts, 'rows': rows, 'limit': 100, 'discovery': dict(discovery) if discovery else None}


def toggle(body, mode='live'):
    if mode != 'live' or set(body) != {'room_url', 'enabled'} or type(body['enabled']) is not bool:
        raise ValueError('房间关注参数无效')
    url = live.room_url(body['room_url'])
    with live.GUARD, app.LOCKS[mode], app.db(mode) as c:
        if not c.execute('SELECT 1 FROM live_rooms WHERE room_url=?', (url,)).fetchone():
            raise ValueError('房间尚未加入直播间库')
        if body['enabled'] and exclusion_reason(c.execute('SELECT title FROM live_rooms WHERE room_url=?',(url,)).fetchone()[0]):
            raise ValueError('该直播间包含手游范围，Mimo 仅承接端游无畏契约')
        c.execute('UPDATE live_rooms SET enabled=? WHERE room_url=?', (int(body['enabled']), url))
    return {'saved': True, 'detail': '房间关注已更新，下批生效；历史弹幕保留'}


def select_room(c):
    return c.execute('SELECT * FROM live_rooms WHERE enabled=1 AND next_check_at<=? '
                     'ORDER BY next_check_at,COALESCE(last_checked_at,\'\'),room_url LIMIT 1', (app.now(),)).fetchone()


def settle(c, session, config):
    row = c.execute('SELECT * FROM live_rooms WHERE room_url=?', (session['room_url'],)).fetchone()
    if not row or (row['last_settled_session_id'] or 0) >= session['id']:
        return
    status = session['status']
    failures = row['failures'] + 1 if status in ('no_data', 'disconnected') else 0
    seconds = (config['offline_retry_minutes'] * 60 if status == 'ended' else
               min(3600, config['empty_retry_minutes'] * 60 * 2 ** min(max(failures - 1, 0), 6)) if failures else
               max(60, config['interval_seconds']))
    c.execute('UPDATE live_rooms SET last_session_id=?,last_settled_session_id=?,last_status=?,last_checked_at=?,next_check_at=?,failures=?, '
              'successful_batches=successful_batches+?,saved_messages=saved_messages+? WHERE room_url=?',
              (session['id'], session['id'], status, session['finished_at'], later(session['finished_at'], seconds), failures,
               int(status == 'completed'), session['inserted'], session['room_url']))


def record_problem(c, session):
    c.execute('UPDATE live_rooms SET last_session_id=?,last_status=?,last_checked_at=? WHERE room_url=?',
              (session['id'], session['status'], session['finished_at'], session['room_url']))


def request_discovery(track_id, config):
    """Network work stays outside the scheduler/stop lock; stopped runs discard results."""
    with app.LOCKS['live'], app.db() as c:
        check = c.execute('SELECT * FROM live_pool_checks WHERE track_id=?', (track_id,)).fetchone()
        if not check or datetime.now(timezone.utc) < datetime.fromisoformat(check['next_discovery_at']):
            return
        stamp = app.now()
        c.execute("UPDATE live_pool_checks SET next_discovery_at=?,discovery_status='reading',discovery_detail=? WHERE track_id=?",
                  (later(stamp, config['discovery_interval_minutes'] * 60), '正在检查公开无畏契约分类', track_id))
    def run():
        import live_discovery
        import live_tracking
        try:
            result = live_discovery.discover(_catalog=False)
        except Exception:
            result = {'status': 'failed', 'detail': '公开分类读取失败，保留房间库，按间隔再检查'}
        with live.GUARD:
            with app.LOCKS['live'], app.db() as c:
                track = c.execute('SELECT * FROM live_tracks WHERE id=?', (track_id,)).fetchone()
                if live_tracking.STOP.is_set() or not track or track['status'] != 'enabled':
                    return
                c.execute('UPDATE live_pool_checks SET discovery_status=?,discovery_detail=?,last_discovery_at=? WHERE track_id=?',
                          (result['status'], result['detail'], app.now(), track_id))
                if result.get('error') in ('http_401', 'http_403', 'http_429'):
                    c.execute("UPDATE live_tracks SET status='attention',next_run_at=NULL,detail=?,updated_at=? WHERE id=?",
                              ('直播分类访问受限，持续监控已暂停；请检查访问状态', app.now(), track_id))
                    stop_id = track['last_session_id']
                else:
                    stop_id = None
                    if result['status'] == 'ready':
                        ingest(c, result.get('rows', []), 'valorant_category', result['fetched_at'])
                        # New candidates may shorten a room cooldown, never the inter-batch gap.
                        if track['next_run_at']:
                            last = c.execute('SELECT finished_at FROM live_sessions WHERE id=?', (track['last_session_id'],)).fetchone()
                            floor = max(app.now(), later(last[0], config['interval_seconds'])) if last and last[0] else app.now()
                            c.execute('UPDATE live_tracks SET next_run_at=MIN(next_run_at,?) WHERE id=?', (floor, track_id))
            if stop_id in live.ACTIVE:
                live.stop(stop_id)
    thread = threading.Thread(target=run, daemon=True, name=f'live-room-discovery-{track_id}')
    try:
        thread.start()
    except Exception:
        with app.LOCKS['live'], app.db() as c:
            c.execute("UPDATE live_pool_checks SET discovery_status='failed',discovery_detail=? WHERE track_id=?",
                      ('发现线程未能启动，按已保存间隔再检查', track_id))
