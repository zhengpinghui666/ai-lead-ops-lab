"""Durable, local fault notifications to an existing Codex task; never collects or sends DMs."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import uuid

BASE = Path(__file__).resolve().parent
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def health(data_dir, port=8765):
    spec = importlib.util.spec_from_file_location('bridge_health', BASE/'scripts/monitor-health.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check(data_dir, port)


def faults(report):
    """Only actionable faults; a deliberate pause/unconfigured channel is not a fault."""
    if report.get('service') != 'running':
        return {'service': {'reason': 'unavailable'}}
    result = {}
    for channel in ('comments', 'live'):
        item = report.get(channel, {})
        reason = item.get('issue', '').partition(':')[2]
        if reason in ('attention', 'task_stalled', 'scheduler_overdue'):
            result[channel] = {'reason': reason, 'task_id': item.get('last_task_id'),
                               'task_status': item.get('task_status')}
    for channel, prefix in (('inbox', 'inbox_sync:'), ('groups', 'groups:')):
        issues = sorted(i for i in report.get('issues', []) if isinstance(i, str) and
                        re.fullmatch(prefix+r'\d+:[a-z_]+', i))
        if issues:
            result[channel] = {'reason': 'read_failure', 'items': issues[:20]}
    if any(str(i).startswith('probe:') for i in report.get('issues', [])):
        result['health'] = {'reason': 'probe_failed'}
    return result


class Bridge:
    def __init__(self, data_dir, *, clock=time.time):
        self.data_dir = Path(data_dir).resolve()
        self.directory = self.data_dir/'private/incident-bridge'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'outbox.db'
        self.clock = clock
        with closing(self.connect()) as c, c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS incidents(
                id TEXT PRIMARY KEY, channel TEXT NOT NULL, fingerprint TEXT NOT NULL,
                metadata TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL, detected_at REAL NOT NULL, settled_at REAL,
                delivery_id TEXT);
              CREATE UNIQUE INDEX IF NOT EXISTS active_channel ON incidents(channel) WHERE active=1;
              CREATE TABLE IF NOT EXISTS deliveries(
                id TEXT PRIMARY KEY, status TEXT NOT NULL, incident_ids TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, queue_id TEXT,
                received_at REAL, finished_at REAL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_try_at REAL NOT NULL DEFAULT 0);
            ''')
            if 'transport' not in {r[1] for r in c.execute('PRAGMA table_info(deliveries)')}:
                c.execute("ALTER TABLE deliveries ADD COLUMN transport TEXT NOT NULL DEFAULT 'queue'")

    def connect(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def configure(self, thread_id, executable):
        if not re.fullmatch(UUID, thread_id) or not Path(executable).is_file():
            raise ValueError('Invalid existing Codex task or executable')
        with closing(self.connect()) as c, c:
            for key, value in dict(thread_id=thread_id, executable=str(Path(executable).resolve()), enabled=True).items():
                c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (key, json.dumps(value)))

    def configure_app(self, node, server, pipe):
        if not Path(node).is_file() or not Path(server).is_file():
            raise ValueError('Installed app adapter paths required')
        import incident_push
        if not str(pipe).startswith(incident_push.PIPE_PREFIX):raise ValueError('Invalid app pipe')
        value=dict(node=str(Path(node).resolve()),server=str(Path(server).resolve()),pipe=str(pipe))
        with closing(self.connect()) as c, c:
            c.execute("INSERT OR REPLACE INTO settings VALUES('app_push',?)",(json.dumps(value),))

    def settings(self):
        with closing(self.connect()) as c:
            return {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}

    def hold(self, seconds):
        if not 0 <= seconds <= 1800:
            raise ValueError('Maintenance hold must be at most 30 minutes')
        with closing(self.connect()) as c, c:
            c.execute("INSERT INTO settings VALUES('hold_until',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps(self.clock()+seconds),))

    def observe(self, report):
        current = faults(report)
        now = self.clock()
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute("SELECT * FROM incidents WHERE active=1 AND channel!='self_test'").fetchall():
                metadata = current.get(row['channel'])
                fingerprint = json.dumps(metadata, sort_keys=True) if metadata else None
                if fingerprint != row['fingerprint']:
                    state = 'recovered' if row['delivery_id'] else 'recovered_before_send'
                    c.execute('UPDATE incidents SET active=0,state=?,settled_at=? WHERE id=?', (state,now,row['id']))
            for channel, metadata in current.items():
                if not c.execute('SELECT 1 FROM incidents WHERE channel=? AND active=1', (channel,)).fetchone():
                    payload = json.dumps(metadata, sort_keys=True)
                    c.execute('INSERT INTO incidents(id,channel,fingerprint,metadata,state,detected_at) VALUES(?,?,?,?,?,?)',
                              (str(uuid.uuid4()),channel,payload,payload,'pending',now))

    def self_test(self):
        now = self.clock()
        with closing(self.connect()) as c, c:
            row = c.execute("SELECT id FROM incidents WHERE channel='self_test' AND active=1").fetchone()
            if row:
                return row['id']
            iid = str(uuid.uuid4())
            c.execute('INSERT INTO incidents(id,channel,fingerprint,metadata,state,detected_at) VALUES(?,?,?,?,?,?)',
                      (iid,'self_test',iid,'{}','pending',now))
            return iid

    def prompt(self, delivery, incidents):
        metadata = [dict(channel=r['channel'], **json.loads(r['metadata'])) for r in incidents]
        test = all(r['channel']=='self_test' for r in incidents)
        python=Path(os.sys.executable)
        if python.name.lower()=='pythonw.exe':python=python.with_name('python.exe')
        command = f"& '{python}' '{BASE / 'scripts/incident-watch.py'}' --data-dir '{self.data_dir}' ack --id {delivery['id']}"
        prefix = 'ClubOps 实时唤醒验收（合成通知，不是真实业务故障）' if test else 'ClubOps 实时故障通知（用户已授权自动诊断修复）'
        return (f'{prefix}\n事件编号：{delivery["id"]}\n项目：{BASE}\n'
                f'固定状态摘要：{json.dumps(metadata, ensure_ascii=False)}\n'
                f'收到后先执行：{command} --state received\n' +
                ('仅验证消息接收，不开启/关闭采集、不发送私信、不修改业务数据；接收后执行下述 resolved 回执。\n' if test else
                 '先只读 data/monitor-health.json 和 SUCCESSOR_HANDOFF.md，核对故障是否仍存在。'
                 '用户授权必要代码修复、相关测试、保留回退备份后正常重启；复用原登录环境。'
                 '不将人工暂停、真实登录/验证码/限流/权限限制当成普通网络故障重试；需要用户处理时明确说明。'
                 '不新增测试私信、补发历史私信或扩大范围。若另有开发正在进行，合并到当前工作，避免同时维护同一服务。\n') +
                f'修复并验证实际批次后执行：{command} --state resolved；'
                f'需要用户处理则 --state needs_user；处理未完成则 --state failed。'
                '回执只写独立通知账本，resolved 会复核原故障确已消失。'
                '不能把消息入队或收到通知表述为故障已修复。')

    def dispatch(self, *, runner=subprocess.run, pusher=None):
        config = self.settings()
        now = self.clock()
        if not config.get('enabled') or config.get('hold_until',0)>now:
            return None
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            # An unacknowledged message must never be replayed after a crash/timeout.
            c.execute("UPDATE deliveries SET status='unknown',error='sender_interrupted_unknown',updated_at=? WHERE status='sending' AND updated_at<?",
                      (now, now-60))
            if c.execute("SELECT 1 FROM deliveries WHERE status IN ('sending','queued','pushed','received','unknown')").fetchone():
                return None
            delivery = c.execute("SELECT * FROM deliveries WHERE status='retry' AND next_try_at<=? ORDER BY created_at LIMIT 1", (now,)).fetchone()
            if delivery:
                ids=json.loads(delivery['incident_ids'])
                rows=[c.execute('SELECT * FROM incidents WHERE id=?',(i,)).fetchone() for i in ids]
                if not any(r['active'] for r in rows):
                    c.execute("UPDATE deliveries SET status='recovered_before_send',updated_at=? WHERE id=?",(now,delivery['id']))
                    return None
                did=delivery['id']
            else:
                rows=c.execute("SELECT * FROM incidents WHERE active=1 AND state='pending' AND delivery_id IS NULL ORDER BY detected_at").fetchall()
                # A service restart is not an incident until absence lasts 15 seconds.
                rows=[r for r in rows if r['channel'] not in ('service','health') or now-r['detected_at']>=15]
                if not rows:
                    return None
                did=str(uuid.uuid4())
                c.execute('INSERT INTO deliveries(id,status,incident_ids,created_at,updated_at) VALUES(?,?,?,?,?)',
                          (did,'sending',json.dumps([r['id'] for r in rows]),now,now))
                for row in rows:
                    c.execute('UPDATE incidents SET delivery_id=? WHERE id=?',(did,row['id']))
            c.execute("UPDATE deliveries SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?",(now,did))
            delivery=dict(c.execute('SELECT * FROM deliveries WHERE id=?',(did,)).fetchone())
        executable=config['executable']
        if not Path(executable).is_file():
            executable=shutil.which('codex') or executable
        status,error,queue_id='unknown','queue_result_unknown',None
        transport='queue'
        app_result=None
        if config.get('app_push'):
            import incident_push
            app_result=(pusher or incident_push.push)(config['app_push'],config['thread_id'],self.prompt(delivery,rows))
            if app_result['status']!='not_sent':
                status,error=app_result['status'],app_result.get('error')
                transport='app_push'
        if app_result is None or app_result['status']=='not_sent':
            try:
                result=runner([executable,'queue','--thread',config['thread_id'],'--message',self.prompt(delivery,rows)],
                    cwd=BASE,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    text=True,encoding='utf8',errors='replace',timeout=20,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                # Do not retain raw stdout/stderr: upstream errors may contain private configuration.
                match=re.search(r'Queued message ('+UUID+r') for thread ('+UUID+r')\.',result.stdout or '')
                if match and match[2]==config['thread_id']:
                    status,error,queue_id='queued',None,match[1]
            except FileNotFoundError:
                status,error='retry','codex_executable_unavailable'  # Process did not start; safe to retry.
            except subprocess.TimeoutExpired:
                error='queue_timeout_unknown'  # May already be accepted: no blind duplicate send.
            except OSError:
                status,error='retry','codex_process_not_started'
        with closing(self.connect()) as c, c:
            # Receipt can arrive while the command is still returning.
            c.execute("UPDATE deliveries SET status=?,error=?,queue_id=?,updated_at=?,next_try_at=?,transport=? WHERE id=? AND status='sending'",
                      (status,error,queue_id,self.clock(),self.clock()+min(300,5*2**min(6,delivery['attempts'])),transport,did))
            c.execute('UPDATE deliveries SET transport=? WHERE id=?',(transport,did))
        return did

    def acknowledge(self, delivery_id, state, report=None):
        if not re.fullmatch(UUID, delivery_id) or state not in ('received','resolved','needs_user','failed'):
            raise ValueError('Invalid acknowledgement')
        with closing(self.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            delivery=c.execute('SELECT * FROM deliveries WHERE id=?',(delivery_id,)).fetchone()
            if not delivery:
                raise ValueError('Unknown delivery')
            rows=[c.execute('SELECT * FROM incidents WHERE id=?',(i,)).fetchone() for i in json.loads(delivery['incident_ids'])]
            if delivery['status'] in ('resolved','needs_user','failed'):
                if delivery['status']==state:
                    return
                raise ValueError('Delivery already completed')
            if state=='resolved':
                if not delivery['received_at']:
                    raise ValueError('Receive the event before resolving it')
                current=faults(report or {})
                if any(r['channel']!='self_test' for r in rows) and ('service' in current or 'health' in current):
                    raise ValueError('Health verification unavailable; cannot confirm recovery')
                if any(r['channel']!='self_test' and r['channel'] in current for r in rows):
                    raise ValueError('Original channel is still faulty')
            now=self.clock()
            if state=='received':
                c.execute("UPDATE deliveries SET status='received',received_at=COALESCE(received_at,?),updated_at=? WHERE id=?",(now,now,delivery_id))
            else:
                c.execute('UPDATE deliveries SET status=?,finished_at=?,updated_at=? WHERE id=?',(state,now,now,delivery_id))
                for row in rows:
                    c.execute('UPDATE incidents SET state=?,active=?,settled_at=? WHERE id=?',
                              (state,0 if state=='resolved' else row['active'],now,row['id']))

    def status(self):
        with closing(self.connect()) as c:
            last=c.execute('SELECT id,status,created_at,updated_at,received_at,finished_at,error,transport FROM deliveries ORDER BY created_at DESC LIMIT 1').fetchone()
            received=c.execute('SELECT 1 FROM deliveries WHERE received_at IS NOT NULL LIMIT 1').fetchone()
            realtime=c.execute("SELECT 1 FROM deliveries WHERE received_at IS NOT NULL AND transport='app_push' LIMIT 1").fetchone()
            pending=c.execute("SELECT COUNT(*) FROM incidents WHERE active=1 AND state='pending'").fetchone()[0]
        return dict(mode='event_push' if self.settings().get('app_push') else 'event_queue',enabled=self.settings().get('enabled',False),
                    realtime_receiver_verified=bool(realtime),
                    receiver_verified=bool(received),pending=pending,last_delivery=dict(last) if last else None)


def feedback(data_dir):
    """Read the watcher heartbeat only; do not create databases during a health check."""
    path=Path(data_dir)/'private/incident-bridge/status.json'
    try:
        raw=json.loads(path.read_text('utf8'))
        fresh=0<=time.time()-raw['heartbeat_at']<45
        last=raw.get('last_delivery') or {}
        ready=fresh and raw.get('enabled') and raw.get('receiver_verified') and not raw.get('watcher_error') and last.get('status') not in ('unknown','retry')
        realtime=bool(ready and raw.get('mode')=='event_push' and raw.get('realtime_receiver_verified') and last.get('transport')=='app_push')
        return dict(mode=raw.get('mode','event_queue'),codex_push_connected=realtime,realtime_connected=realtime,
                    watcher_running=fresh,receiver_verified=bool(raw.get('receiver_verified')),
                    last_delivery=raw.get('last_delivery'),pending=raw.get('pending',0))
    except (OSError,ValueError,KeyError,TypeError):
        return dict(mode='local_only',codex_push_connected=False)
