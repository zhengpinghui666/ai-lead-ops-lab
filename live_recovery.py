"""Explicit repair of a diagnosed pre-response navigation failure.

This is not a catch-all resume: the old session, account, room and track remain
the authority. A deterministic request ID makes repeated repair requests inert.
"""
import json
import clubops as app

TRANSIENT = {'ERR_FAILED', 'ERR_CONNECTION_CLOSED', 'ERR_CONNECTION_RESET',
             'ERR_CONNECTION_ABORTED', 'ERR_CONNECTION_TIMED_OUT', 'ERR_TIMED_OUT',
             'ERR_INTERNET_DISCONNECTED', 'ERR_NETWORK_CHANGED', 'ERR_NAME_NOT_RESOLVED'}


def eligible(c, session):
    if not session or session['status'] != 'failed' or not session['finished_at']:
        return False
    import collection_session_refresh
    if collection_session_refresh.legacy_expiry(c,session):return True
    row = c.execute("SELECT detail FROM live_session_events WHERE session_id=? AND status='diagnostic' ORDER BY id DESC LIMIT 1", (session['id'],)).fetchone()
    try:
        info = json.loads(row[0].split('：', 1)[1])
        page, net = info['page'], info['network']
        attempts = net.get('navigation_attempts', 1)
        return bool(page == dict(navigation='failed', http_status=None, body_chars=0, gate='unknown')
            and type(attempts) is int and 1 <= attempts <= 3
            and net['requests'] == attempts and net['failures']
            and set(net['failures']) <= TRANSIENT and sum(net['failures'].values()) == attempts
            and not net['script_errors'] and not net['live_responses']
            and not info['methods'] and not info['errors'] and not info['sockets']
            and not info['poll']['responses'] and not session['observed'] and not session['frames'])
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        return False


def retry(body, mode='live'):
    import live_monitor as live
    if mode != 'live' or set(body) != {'id', 'session_id'} or any(type(v) is not int or v < 1 for v in body.values()):
        raise ValueError('恢复仅接受原跟踪与原失败批次 ID')
    request_id = f"connection-recovery-{body['id']}-{body['session_id']}"
    with live.GUARD:
        with app.LOCKS[mode], app.db(mode) as c:
            old = c.execute('SELECT id,status FROM live_sessions WHERE request_id=?', (request_id,)).fetchone()
            if old:
                return dict(old)
            track = c.execute('SELECT * FROM live_tracks ORDER BY id DESC LIMIT 1').fetchone()
            session = c.execute('SELECT * FROM live_sessions WHERE id=?', (body['session_id'],)).fetchone()
            if (not track or track['id'] != body['id'] or track['status'] != 'attention'
                or track['last_session_id'] != body['session_id'] or live.ACTIVE
                or not eligible(c, session)):
                raise ValueError('原跟踪状态已变化，或不是可重连的导航故障')
            config = json.loads(session['config'])
        result = live.start({'request_id': request_id}, _tracking_config=config, _retry_from=body['session_id'])
        with app.LOCKS[mode], app.db(mode) as c:
            c.execute("UPDATE live_tracks SET status='enabled',last_session_id=?,run_count=run_count+1,next_run_at=NULL,detail=?,updated_at=? WHERE id=?",
                (result['id'], f"连接修复：原批次 #{body['session_id']} → #{result['id']}，等待实际读取结果", app.now(), body['id']))
            c.execute('UPDATE live_rooms SET last_session_id=?,last_status=? WHERE room_url=?',
                      (result['id'], result['status'], session['room_url']))
        return result
