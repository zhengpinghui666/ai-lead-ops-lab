"""Bounded live chat sessions, independent of historical video comments and DMs."""
import hashlib
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

import clubops as app
import collector
import comment_filters

GUARD = threading.RLock()
ACTIVE = {}
DEFAULTS = dict(room_url='', duration_seconds=60, max_messages=100, include_keywords='', exclude_keywords='', interactive=False, interval_seconds=30,
                discovery_interval_minutes=15, offline_retry_minutes=15, empty_retry_minutes=5)
DETAILS = {
    'connecting': '正在打开专用直播页面，尚未收到有效弹幕数据',
    'running': '已收到直播连接数据；只记录当前观察到的文字弹幕',
    'reconnected': '直播连接已恢复，中断期间存在数据缺口，未补齐',
    'disconnected': '直播连接中断，数据可能缺失；浏览器可能重新连接',
    'completed': '已达到读取时长或消息预算，已结束本次监控',
    'cancelled': '已停止本次直播监控，已有记录保留',
    'interrupted': '监控中断，已有记录保留；不会自动恢复',
    'needs_login': '页面要求登录；本次已结束，请在专用会话登录后手动重新开启',
    'needs_verification': '页面要求人工验证；本次已结束，不自动重试',
    'ended': '观察到直播结束状态，已停止读取',
    'no_data': '观察期没有取得有效文字弹幕；不代表直播间没有弹幕',
    'dependency_missing': '未找到 Node、Playwright 或可用的 Chrome',
    'rate_limited': '平台返回访问频率限制，本次已停止',
    'access_denied': '平台拒绝访问，本次已停止',
    'resource_limited': '本地处理达到资源上限，本次停止，可能有未保存数据',
    'schema_changed': '响应结构无法可靠识别，本次停止，可能有数据缺口',
    'room_changed': '页面或连接房间发生变化，停止以防串入其他房间数据',
    'failed': '直播读取失败，已有记录保留；请检查运行依赖或页面状态',
    'stopping': '正在关闭本次直播会话，停止接收新记录',
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS live_sessions (
 id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, room_url TEXT NOT NULL, room_id TEXT,
 config TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, observed INTEGER NOT NULL DEFAULT 0,
 inserted INTEGER NOT NULL DEFAULT 0, duplicate INTEGER NOT NULL DEFAULT 0, filtered INTEGER NOT NULL DEFAULT 0,
 invalid INTEGER NOT NULL DEFAULT 0, gaps INTEGER NOT NULL DEFAULT 0, frames INTEGER NOT NULL DEFAULT 0,
 started_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS live_messages (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES live_sessions(id), room_id TEXT NOT NULL,
 message_id TEXT, outer_message_id TEXT, uid TEXT, nickname TEXT NOT NULL, raw_text TEXT NOT NULL, published_at TEXT, observed_at TEXT NOT NULL,
 filter_reason TEXT NOT NULL, category TEXT NOT NULL, analysis_method TEXT NOT NULL, reason TEXT NOT NULL,
 facts TEXT NOT NULL, include_matches TEXT NOT NULL, exclude_matches TEXT NOT NULL, payload_hash TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_live_message_identity ON live_messages(room_id,message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_live_message_session ON live_messages(session_id,id);
CREATE TABLE IF NOT EXISTS live_session_events (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES live_sessions(id), status TEXT NOT NULL,
 detail TEXT NOT NULL, observed_at TEXT NOT NULL
);
'''


def room_url(value):
    if not isinstance(value, str):
        raise ValueError('请填写直播间完整链接或房间网页数字 ID')
    value = value.strip()
    if re.fullmatch(r'[1-9][0-9]{2,29}', value):
        return 'https://live.douyin.com/' + value
    u = urlparse(value)
    if u.scheme != 'https' or u.hostname != 'live.douyin.com' or u.port not in (None, 443) or u.username or u.password or not re.fullmatch(r'/[1-9][0-9]{2,29}/?', u.path):
        raise ValueError('需要 https://live.douyin.com/房间网页ID 完整链接，暂不解析短链')
    return 'https://live.douyin.com' + u.path.rstrip('/')


def options(body):
    from monitoring import integer
    interactive = body.get('interactive', False)
    if type(interactive) is not bool:
        raise ValueError('显示浏览器须为布尔值')
    return dict(room_url=room_url(body.get('room_url')), duration_seconds=integer(body.get('duration_seconds', 60), '读取时长', 10, 3600),
                max_messages=integer(body.get('max_messages', 100), '消息预算', 1, 5000),
                include_keywords=comment_filters.normalize(body.get('include_keywords', ''), '弹幕关键词'),
                exclude_keywords=comment_filters.normalize(body.get('exclude_keywords', ''), '弹幕屏蔽词'), interactive=interactive,
                interval_seconds=integer(body.get('interval_seconds', 30), '批次间隔', 30, 3600),
                discovery_interval_minutes=integer(body.get('discovery_interval_minutes', 15), '直播分类检查间隔', 5, 120),
                offline_retry_minutes=integer(body.get('offline_retry_minutes', 15), '下播房间复查间隔', 5, 120),
                empty_retry_minutes=integer(body.get('empty_retry_minutes', 5), '未取得数据复查间隔', 1, 60))


def settings(mode='live'):
    with app.db(mode) as c:
        row = c.execute("SELECT value FROM settings WHERE key='live_monitor'").fetchone()
    return {**DEFAULTS, **json.loads(row[0])} if row else dict(DEFAULTS)


def save(body, mode='live'):
    if mode != 'live':
        raise ValueError('演示区不配置真实直播读取')
    value = options(body)
    with app.LOCKS[mode], app.db(mode) as c:
        c.execute("INSERT INTO settings VALUES('live_monitor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value, ensure_ascii=False),))
    return {'saved': True, 'detail': '直播配置已保存；不会启动或改写正在运行的会话参数'}


def state(mode='live'):
    with GUARD, app.db(mode) as c:
        sessions = [dict(r) for r in c.execute('SELECT * FROM live_sessions ORDER BY id DESC LIMIT 20')]
        active = next(iter(ACTIVE), None) if mode == 'live' else None
        current = next((s for s in sessions if s['id'] == active), sessions[0] if sessions else None)
        latest_message_id = c.execute('SELECT COALESCE(MAX(id),0) FROM live_messages').fetchone()[0]
        import live_workflow
        records = live_workflow.records(c, 'WHERE m.session_id=?', (current['id'],)) if current else []
        events = [dict(r) for r in c.execute('SELECT * FROM live_session_events WHERE session_id=? ORDER BY id DESC LIMIT 30', (current['id'],))] if current else []
    for row in sessions:
        row['config'] = json.loads(row['config'])
    import live_tracking
    import live_discovery
    import live_rules
    import live_room_pool
    return dict(config=settings(mode), sessions=sessions, current=current, rows=records, events=events, active_id=active,
                transport='browser_live', ruleset_version=live_rules.RULESET_VERSION, history_available=True, records_limit=500, latest_message_id=latest_message_id,
                tracking=live_tracking.state(mode), discovery=live_discovery.state(mode), library=live_room_pool.state(mode))


def history(query, mode='live'):
    """Read local archived messages, including records outside the latest batch."""
    from monitoring import integer
    import live_workflow
    offset = integer(query.get('offset', 0), '分页起点', 0, 10000000)
    limit = integer(query.get('limit', 25), '每页条数', 1, 100)
    search = query.get('q', '')
    status = query.get('filter', 'all')
    if not isinstance(search, str) or len(search) > 200 or status not in ('all', 'accepted', 'filtered', 'valuable'):
        raise ValueError('弹幕筛选条件无效')
    clauses, args = ['1=1'], []
    sid = query.get('session_id', '')
    if sid not in ('', None, 'all'):
        sid = integer(sid, '直播会话', 1, 2**63 - 1)
        clauses.append('m.session_id=?')
        args.append(sid)
    if search.strip():
        clauses.append('(instr(lower(m.raw_text),lower(?))>0 OR instr(lower(m.nickname),lower(?))>0 OR instr(m.uid,?)>0 OR instr(m.message_id,?)>0)')
        args.extend([search.strip()] * 4)
    where = ' WHERE ' + ' AND '.join(clauses)
    with app.LOCKS[mode], app.db(mode) as c:
        anchor = query.get('anchor_id')
        anchor = integer(anchor, '存档快照', 0, 2**63 - 1) if anchor not in (None, '') else c.execute('SELECT COALESCE(MAX(id),0) FROM live_messages').fetchone()[0]
        where += ' AND m.id<=?'
        args.append(anchor)
        counts = dict(c.execute("SELECT COUNT(*) AS observed,COALESCE(SUM(m.filter_reason=''),0) AS accepted,COALESCE(SUM(m.filter_reason<>''),0) AS filtered FROM live_messages m" + where, args).fetchone())
        # Resolve the model once for this response, not once for every message.
        engine = live_workflow.connection_engine(c)
        projected, valuable = {}, []
        for raw in c.execute(live_workflow.SELECT + where + " AND m.filter_reason='' ORDER BY m.id DESC", args):
            row = live_workflow.project(c, raw, history=False, model_engine=engine)
            projected[row['id']] = row
            if row['category'] == 'buyer' and row['analysis_method'] in ('rules','model','human'):
                valuable.append(row)
        counts['valuable'] = len(valuable)
        total = counts[{'all':'observed','accepted':'accepted','filtered':'filtered','valuable':'valuable'}[status]]
        if status == 'valuable':
            rows = valuable[offset:offset+limit]
        else:
            if status != 'all':
                where += " AND m.filter_reason=''" if status == 'accepted' else " AND m.filter_reason<>''"
            rows = [projected[r['id']] if r['id'] in projected else live_workflow.project(c, r, history=False, model_engine=engine) for r in c.execute(
                live_workflow.SELECT + where + ' ORDER BY m.id DESC LIMIT ? OFFSET ?', (*args, limit, offset))]
    return dict(rows=rows, total=total, offset=offset, limit=limit, has_more=offset + len(rows) < total,
                scope='local_archive', platform_history_available=False, anchor_id=anchor, counts=counts)


def record_status(session_id, status, final=False, **counts):
    if status not in DETAILS:
        raise ValueError('未知直播状态')
    with app.LOCKS['live'], app.db() as c:
        row = c.execute('SELECT * FROM live_sessions WHERE id=?', (session_id,)).fetchone()
        if not row or row['finished_at'] or row['status'] == 'stopping' and not final:
            return
        gap = int(status == 'disconnected' and row['status'] != 'disconnected'
                  or status == 'reconnected' and row['status'] not in ('disconnected', 'reconnected'))
        c.execute('UPDATE live_sessions SET status=?,detail=?,updated_at=?,finished_at=?,gaps=gaps+?,frames=?,invalid=? WHERE id=?',
                  (status, DETAILS[status], app.now(), app.now() if final else None, gap, counts.get('frames', row['frames']), row['invalid'] + counts.get('invalid', 0), session_id))
        if row['status'] != status or final:
            c.execute('INSERT INTO live_session_events(session_id,status,detail,observed_at) VALUES(?,?,?,?)', (session_id, status, DETAILS[status], app.now()))
        if final:
            import live_room_pool
            final_row = c.execute('SELECT * FROM live_sessions WHERE id=?', (session_id,)).fetchone()
            live_room_pool.settle(c, final_row, {**DEFAULTS, **json.loads(final_row['config'])})


def identifier(value, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) >= 2**63:
        raise ValueError('数字身份字段无效')
    return value


def receive(session_id, message):
    """Only the local child pipe calls this; no external ingest API exists."""
    with app.LOCKS['live'], app.db() as c:
        session = c.execute('SELECT * FROM live_sessions WHERE id=?', (session_id,)).fetchone()
        if not session or session['finished_at']:
            return
        if message.get('type') == 'diagnostic':
            methods = message.get('methods')
            if not isinstance(methods, dict) or len(methods) > 100 or any(not re.fullmatch(r'(?:Webcast[A-Za-z0-9]{1,70}Message|unknown)', k) or type(v) is not int or not 0 <= v <= 1000000 for k, v in methods.items()):
                raise ValueError('诊断数据格式无效')
            errors = message.get('errors', {})
            if not isinstance(errors, dict) or len(errors) > 10 or any(k not in ('room_mismatch', 'message_id_mismatch', 'ambiguous_field', 'bad_text', 'bad_nickname', 'decode_error') or type(v) is not int or not 0 <= v <= 1000000 for k, v in errors.items()):
                raise ValueError('错误计数无效')
            timestamps = message.get('timestamps', {})
            if not isinstance(timestamps, dict) or any(k not in ('seconds', 'milliseconds', 'missing', 'invalid') or type(v) is not int or not 0 <= v <= 1000000 for k, v in timestamps.items()):
                raise ValueError('时间字段诊断无效')
            page = message.get('page', {})
            sockets = message.get('sockets', [])
            if not isinstance(page, dict) or page and (set(page) != {'navigation','http_status','body_chars','gate'}
                         or page['navigation'] not in ('not_started','loading','loaded','timeout','failed')
                         or page['gate'] not in ('unknown','none','verification','login','ended','chat_unavailable')
                         or type(page['body_chars']) is not int or not 0 <= page['body_chars'] <= 25000
                         or page['http_status'] is not None and (type(page['http_status']) is not int or not 100 <= page['http_status'] <= 599)):
                raise ValueError('页面诊断无效')
            if not isinstance(sockets, list) or len(sockets) > 8 or any(not isinstance(v,dict) or set(v) != {'host','path'}
                    or not isinstance(v.get('host'), str) or not isinstance(v.get('path'), str)
                    or not re.fullmatch(r'[a-z0-9.-]{1,100}',v['host']) or not re.fullmatch(r'[/a-zA-Z0-9_-]{1,200}',v['path']) for v in sockets):
                raise ValueError('连接诊断无效')
            network = message.get('network', {})
            if not isinstance(network, dict) or network and (set(network) not in ({'requests','failures','script_errors','live_responses'}, {'requests','failures','script_errors','live_responses','live_routes'})
                    or type(network['requests']) is not int or not 0 <= network['requests'] <= 1000000):
                raise ValueError('网络诊断无效')
            for key, pattern in [('failures', r'(?:ERR_[A-Z_]{1,50}|other)'), ('script_errors', r'(?:TypeError|ReferenceError|SyntaxError|RangeError|Error|NotSupportedError|NotAllowedError|AbortError|SecurityError|NetworkError|InvalidStateError|other)'), ('live_responses', r'[1-5][0-9]{2}'), ('live_routes', r'/webcast/[a-zA-Z0-9/_-]{1,100}')]:
                group = network.get(key, {})
                if not isinstance(group, dict) or len(group) > 20 or any(not re.fullmatch(pattern, k) or type(v) is not int or not 0 <= v <= 1000000 for k, v in group.items()):
                    raise ValueError('网络诊断计数无效')
            poll = message.get('poll', {})
            if not isinstance(poll, dict) or any(k not in ('responses','decoded','missing_room','decode_errors','body_errors','oversized','empty_bodies','empty_messages','max_bytes') or type(v) is not int or not 0 <= v <= (4*1024*1024 if k == 'max_bytes' else 1000000) for k,v in poll.items()):
                raise ValueError('轮询诊断计数无效')
            poll_fields = message.get('poll_fields', {})
            if not isinstance(poll_fields, dict) or len(poll_fields) > 20 or any(not re.fullmatch(r'f[1-9][0-9]{0,8}', k) or int(k[1:]) >= 2**29 or type(v) is not int or not 0 <= v <= 1000000 for k,v in poll_fields.items()):
                raise ValueError('轮询字段诊断无效')
            c.execute('INSERT INTO live_session_events(session_id,status,detail,observed_at) VALUES(?,?,?,?)', (session_id, 'diagnostic', '连接、页面与消息计数：' + json.dumps(dict(methods=methods, errors=errors, timestamps=timestamps, page=page, sockets=sockets, network=network, poll=poll, poll_fields=poll_fields), ensure_ascii=False), app.now()))
            return
        if session['status'] == 'stopping':
            return
        if message.get('type') == 'stream':
            room = identifier(message.get('room_id'))
            if session['room_id'] and session['room_id'] != room:
                raise ValueError('连接切换房间')
            transport = message.get('transport', 'browser_websocket')
            if transport not in ('browser_websocket', 'browser_http_poll'):
                raise ValueError('直播读取通道无效')
            c.execute('UPDATE live_sessions SET room_id=?,updated_at=? WHERE id=?', (room, app.now(), session_id))
            detail = '实际读取通道：' + ('浏览器页面 HTTP 轮询响应（非独立后端请求）' if transport == 'browser_http_poll' else '浏览器页面 WebSocket 入站数据')
            c.execute('INSERT INTO live_session_events(session_id,status,detail,observed_at) VALUES(?,?,?,?)', (session_id, 'transport', detail, app.now()))
            return
        row = message.get('record', {})
        config = json.loads(session['config'])
        if not isinstance(row, dict) or message.get('type') != 'message':
            raise ValueError('直播事件无效')
        room, mid, uid = identifier(row.get('room_id')), identifier(row.get('message_id'), True), identifier(row.get('uid'), True)
        outer_id = identifier(row.get('outer_message_id'), True)
        if room != session['room_id'] or session['observed'] >= config['max_messages']:
            raise ValueError('直播记录超出房间或预算')
        text = row.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('弹幕原文无效')
        published = row.get('published_at')
        if published is not None:
            try:
                stamp = datetime.fromisoformat(published.replace('Z', '+00:00'))
                if not stamp.tzinfo or not 2010 <= stamp.year < 2100:
                    raise ValueError()
                published = stamp.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('发布时间无效')
        digest = hashlib.sha256(json.dumps([room, mid, uid, text, published], ensure_ascii=False).encode()).hexdigest()
        previous = c.execute('SELECT payload_hash FROM live_messages WHERE room_id=? AND message_id=?', (room, mid)).fetchone() if mid else None
        if previous:
            counter = 'duplicate' if previous[0] == digest else 'invalid'
            c.execute(f'UPDATE live_sessions SET observed=observed+1,{counter}={counter}+1,updated_at=? WHERE id=?', (app.now(), session_id))
            return
        rejected = comment_filters.rejection(text, config['include_keywords'], config['exclude_keywords']) or ''
        import live_rules
        result = live_rules.classify(text) if not rejected else dict(category='uncertain', analysis_method='not_analyzed', reason='按本次固定关键词配置过滤，未作需求判断', facts={})
        message_id = c.execute('INSERT INTO live_messages(session_id,room_id,message_id,outer_message_id,uid,nickname,raw_text,published_at,observed_at,filter_reason,category,analysis_method,reason,facts,include_matches,exclude_matches,payload_hash,game) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (session_id, room, mid, outer_id, uid, app.clean(row.get('nickname'), 200), text, published, app.now(), rejected, result['category'], result['analysis_method'], result['reason'], json.dumps(result['facts'], ensure_ascii=False),
                   json.dumps(comment_filters.matches(text, config['include_keywords']), ensure_ascii=False), json.dumps(comment_filters.matches(text, config['exclude_keywords']), ensure_ascii=False), digest, result.get('game', ''))).lastrowid
        import live_workflow
        live_workflow.attach(c, message_id)
        import asset_verticality
        asset_verticality.refresh(c,'live',session['room_url'])
        import analysis_store
        analysis_store.capture_rule(c, 'live', message_id)
        c.execute('UPDATE live_sessions SET observed=observed+1,inserted=inserted+1,filtered=filtered+?,updated_at=? WHERE id=?', (int(bool(rejected)), app.now(), session_id))
    if not rejected:
        import semantic_queue
        semantic_queue.enqueue('live', [message_id])


def start(body, mode='live', *, _tracking_config=None):
    if mode != 'live':
        raise ValueError('演示区不连接真实直播间')
    request_id = body.get('request_id')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
        raise ValueError('缺少本次启动的唯一请求 ID')
    config = options(_tracking_config if _tracking_config is not None else settings())
    with GUARD, app.LOCKS['live'], app.db() as c:
        old = c.execute('SELECT id,status FROM live_sessions WHERE request_id=?', (request_id,)).fetchone()
        if old:
            return dict(old)
        if ACTIVE:
            raise ValueError('已有直播会话；请先停止或等待完成')
        if _tracking_config is None and c.execute("SELECT 1 FROM live_tracks WHERE status='enabled'").fetchone():
            raise ValueError('持续跟踪已开启，请先关闭后再启动单次读取')
        sid = c.execute('INSERT INTO live_sessions(request_id,room_url,config,status,detail,started_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (request_id, config['room_url'], json.dumps(config, ensure_ascii=False), 'connecting', DETAILS['connecting'], app.now(), app.now())).lastrowid
        control = {'process': None, 'stop': False, 'thread': None}
        ACTIVE[sid] = control
    thread = threading.Thread(target=worker, args=(sid, config, control), daemon=True, name=f'live-{sid}')
    control['thread'] = thread
    try:
        thread.start()
    except Exception:
        with GUARD:
            ACTIVE.pop(sid, None)
        record_status(sid, 'failed', final=True)
        raise
    return {'id': sid, 'status': 'connecting'}


def stop(session_id, mode='live'):
    if mode != 'live' or type(session_id) is not int:
        raise ValueError('直播停止参数无效')
    with GUARD:
        control = ACTIVE.get(session_id)
        if not control:
            return {'id': session_id, 'detail': '会话已结束，不会重新启动'}
        control['stop'] = True
        record_status(session_id, 'stopping')
        process = control['process']
        if process and process.poll() is None:
            try:
                process.stdin.write('{"command":"stop"}\n')
                process.stdin.flush()
            except (OSError, ValueError):
                pass
    return {'id': session_id, 'detail': '正在停止本次直播会话；不影响评论采集'}


def worker(session_id, config, control):
    process, deadline = None, None
    final = 'interrupted'
    counts = {}
    try:
        node, package = collector.dependencies()
        if not node or not package.is_dir():
            final = 'dependency_missing'
            return
        env = {**os.environ, 'CLUBOPS_PLAYWRIGHT': str(package)}
        process = subprocess.Popen([node, str(collector.BASE / 'live_runner.cjs')], cwd=collector.BASE, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = threading.Timer(config['duration_seconds'] + 60, process.kill)
        deadline.daemon = True
        deadline.start()
        with GUARD:
            control['process'] = process
            process.stdin.write(json.dumps({**config, 'profile_dir': str(app.DATA_DIR / 'live-browser-profile')}) + '\n')
            if control['stop']:
                process.stdin.write('{"command":"stop"}\n')
            process.stdin.flush()
        while True:
            line = process.stdout.readline(65537)
            if not line:
                break
            if len(line) > 65536 or not line.endswith('\n'):
                final = 'resource_limited'
                break
            value = json.loads(line)
            kind = value.get('type')
            if kind in ('stream', 'message', 'diagnostic'):
                receive(session_id, value)
            elif kind == 'status':
                record_status(session_id, value['status'])
            elif kind == 'final':
                if value.get('status') not in DETAILS:
                    raise ValueError()
                final = value['status']
                counts = {k: int(value[k]) for k in ('frames', 'invalid') if type(value.get(k)) is int and 0 <= value[k] <= 10000000}
        process.wait(timeout=5)
    except FileNotFoundError:
        final = 'dependency_missing'
    except Exception:
        final = 'failed'
    finally:
        if deadline:
            deadline.cancel()
        if process:
            if process.poll() is None:
                try:
                    process.stdin.write('{"command":"stop"}\n')
                    process.stdin.flush()
                    process.wait(timeout=12)
                except Exception:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
        record_status(session_id, 'cancelled' if control['stop'] else final, final=True, **counts)
        with GUARD:
            ACTIVE.pop(session_id, None)


def recover():
    with app.LOCKS['live'], app.db() as c:
        ids = [r[0] for r in c.execute('SELECT id FROM live_sessions WHERE finished_at IS NULL')]
    for sid in ids:
        record_status(sid, 'interrupted', final=True)


def shutdown():
    with GUARD:
        sessions = list(ACTIVE.items())
    for sid, control in sessions:
        stop(sid)
    for sid, control in sessions:
        if control['thread']:
            control['thread'].join(timeout=15)
