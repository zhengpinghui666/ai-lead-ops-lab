from __future__ import annotations

import json
import gzip
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import clubops
import collector
import collection_scheduler
import messaging_http
import monitoring
import uid_messaging
import intent_outreach
import uid_inbox_sync
import live_monitor
import live_tracking
import live_workflow
import analysis_store
import semantic
import semantic_queue
import runtime
import login_recovery
import login_relay
import team_access
import socket
import threading
import uuid
import sys

BASE = Path(__file__).resolve().parent
HOST = '127.0.0.1'
PORT = int(os.getenv('LEADOPS_PORT', '8765'))
CSRF = secrets.token_urlsafe(32)


def collection_state(mode):
    import monitor_board
    result = collector.state(mode)
    result['plans'] = collection_scheduler.state(mode)
    result['monitor'] = monitoring.state(mode)
    result['results'] = monitoring.results(mode)
    result['board'] = monitor_board.build(result['tasks'],result['plans'],mode)
    result['live_monitor'] = live_monitor.state(mode)
    result['model_queue'] = semantic_queue.state(mode)
    result['login_recovery'] = login_recovery.state(mode)
    import discovery_tracking
    result['discovery'] = discovery_tracking.state(mode)
    result['inbox_sync'] = uid_inbox_sync.state(mode)
    result['intent_outreach'] = intent_outreach.state(mode)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = 'ClubOps/2.0'
    protocol_version = 'HTTP/1.1'

    def respond(self, data, status=200, mime='application/json; charset=utf-8'):
        raw = json.dumps(data, ensure_ascii=False).encode() if mime.startswith('application/json') else data
        compressed = False
        encodings = [v.strip().lower() for v in self.headers.get('Accept-Encoding','').split(',')]
        if mime.startswith('application/json') and len(raw) >= 1024 and 'gzip' in encodings:
            raw = gzip.compress(raw, compresslevel=1, mtime=0)
            compressed = True
        self.send_response(status)
        self.send_header('Vary','Accept-Encoding')
        if compressed:
            self.send_header('Content-Encoding','gzip')
        for key, value in {'Content-Type': mime, 'Content-Length': str(len(raw)), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get('Host') in {f'localhost:{port}', f'127.0.0.1:{port}'}

    def mode(self):
        mode = parse_qs(urlparse(self.path).query).get('mode', ['live'])[0]
        if mode not in ('live', 'demo'):
            raise ValueError('工作区无效')
        if mode == 'demo':
            clubops.seed_demo()
        return mode

    def do_GET(self):
        if not self.valid_host():
            return self.respond({'error': 'Host not allowed'}, 403)
        try:
            path = urlparse(self.path).path
            if path == '/api/service':
                return self.respond(self.server.service_state())
            if path == '/api/uid-inbox':
                import uid_inbox_store
                query=parse_qs(urlparse(self.path).query)
                return self.respond(uid_inbox_store.history(int(query.get('lead_id',['0'])[0]),self.mode(),int(query.get('before',['0'])[0])))
            if path == '/api/asset-references':
                import asset_references
                return self.respond(asset_references.state(self.mode()))
            if path in ('/api/asset-keywords','/api/asset-keyword'):
                import asset_keywords
                query=parse_qs(urlparse(self.path).query)
                return self.respond(asset_keywords.state(self.mode(),query.get('q',[''])[0],query.get('scope',['all'])[0],query.get('status',['all'])[0]) if path=='/api/asset-keywords' else asset_keywords.detail(query.get('term',[''])[0],self.mode()))
            if path == '/api/login-recovery':
                return self.respond({**login_recovery.state(self.mode()), 'csrf': CSRF})
            if path == '/api/collector':
                return self.respond(collection_state(self.mode()))
            if path == '/api/monitor-comments':
                import monitor_comments
                query = {k:v[0] for k,v in parse_qs(urlparse(self.path).query).items()}
                return self.respond(monitor_comments.history(query, self.mode()))
            if path == '/api/live-message':
                query = parse_qs(urlparse(self.path).query)
                return self.respond(live_workflow.detail(query.get('id', [''])[0], self.mode()))
            if path == '/api/live-history':
                query = {k:v[0] for k,v in parse_qs(urlparse(self.path).query).items()}
                return self.respond(live_monitor.history(query, self.mode()))
            if path == '/api/analysis-history':
                query = parse_qs(urlparse(self.path).query)
                return self.respond(analysis_store.history(query.get('evidence_type', [''])[0], int(query.get('id', ['0'])[0]), self.mode()))
            if path == '/api/lead-live-history':
                query = parse_qs(urlparse(self.path).query)
                return self.respond(live_workflow.history(query.get('lead_id', [''])[0], query.get('offset', ['0'])[0], self.mode()))
            if path == '/api/collector-evidence':
                mode = self.mode()
                task_id = int(parse_qs(urlparse(self.path).query).get('id', ['0'])[0])
                with clubops.db(mode) as c:
                    rows = [dict(r) for r in c.execute('SELECT * FROM collection_observations WHERE task_id=? ORDER BY observed_at,kind,external_id LIMIT 1000', (task_id,))]
                    diagnostics = [dict(r) for r in c.execute('SELECT * FROM collection_diagnostics WHERE task_id=? ORDER BY id', (task_id,))]
                for d in diagnostics:
                    d['snapshot'] = json.loads(d['snapshot'])
                return self.respond({'observations': rows, 'diagnostics': diagnostics})
            if path == '/api/state':
                result = clubops.state(self.mode())
                result['messaging_test'] = messaging_http.state(clubops.DATA_DIR, result['mode'])
                result['uid_messaging'] = uid_messaging.state(result['mode'])
                result['csrf'] = CSRF
                result['collector'] = collection_state(result['mode'])
                result['connections']['collector'] = 'observed' if result['collector']['last_received'] else 'unverified'
                return self.respond(result)
            if path == '/api/export':
                result = clubops.state(self.mode())
                return self.respond({'exported_at': clubops.now(), 'mode': result['mode'], 'comments': result['comments'], 'leads': result['leads']})
            files = {'/': ('app.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/app.css': ('app.css', 'text/css; charset=utf-8'), '/vendor/lucide.min.js': ('vendor/lucide.min.js', 'text/javascript; charset=utf-8'),
                     '/login': ('login.html', 'text/html; charset=utf-8'), '/login.js': ('login.js', 'text/javascript; charset=utf-8'),
                     '/login.css': ('login.css', 'text/css; charset=utf-8'), '/login-guide': ('login-guide.html', 'text/html; charset=utf-8'), '/iphone-script': ('clubops-iphone.js', 'text/plain; charset=utf-8')}
            if path not in files:
                return self.respond({'error': 'Not found'}, 404)
            file, mime = files[path]
            self.respond((BASE / 'static' / file).read_bytes(), mime=mime)
        except ValueError as exc:
            self.respond({'error': str(exc)}, 400)

    def do_POST(self):
        if not hasattr(self.server, 'lifecycle_lock'):
            return self.handle_post()
        with self.server.lifecycle_lock:
            if self.server.stopping:
                return self.respond({'error': '服务正在正常关闭，暂不接受写入'}, 503)
            self.server.active_writes += 1
        try:
            return self.handle_post()
        finally:
            with self.server.lifecycle_lock:
                self.server.active_writes -= 1

    def handle_post(self):
        origin = self.headers.get('Origin')
        port = self.server.server_port
        if not self.valid_host() or self.headers.get('X-ClubOps-Token') != CSRF or (origin and origin not in {f'http://localhost:{port}', f'http://127.0.0.1:{port}'}):
            return self.respond({'error': '请求验证失败，请刷新页面'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 2_000_000:
                raise ValueError('请求大小必须在 1 字节到 2 MB 之间')
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(body, dict):
                raise ValueError('请求必须是 JSON 对象')
            path = urlparse(self.path).path
            if not path.startswith('/api/'):
                raise ValueError('接口不存在')
            action = path[5:]
            if action == 'service-stop':
                if parse_qs(urlparse(self.path).query).get('mode', ['live']) != ['live'] or body != {'instance_id': self.server.instance_id}:
                    raise ValueError('服务实例已变化或停止参数无效；未停止')
                self.server.prepare_stop()
                try:
                    self.respond({'ok': True, 'status': 'stopping', 'pid': os.getpid()}, 202)
                finally:
                    # shutdown must run outside serve_forever's thread. main's
                    # finally blocks settle workers, then release the data lock.
                    threading.Thread(target=self.server.shutdown, name='service-shutdown', daemon=True).start()
                return
            mode = self.mode()
            if action.startswith('login-'):
                if mode != 'live':
                    raise ValueError('登录恢复仅用于正式工作区')
                if action == 'login-recovery-save':
                    result = login_recovery.save(body)
                elif action == 'login-recovery-start':
                    result = login_recovery.start(body)
                elif action in ('login-recovery-cancel', 'login-recovery-complete'):
                    result = login_recovery.command(body, action.removeprefix('login-recovery-'))
                elif action == 'login-relay-bind':
                    if set(body) != {'origin'} or login_recovery.busy():
                        raise ValueError('中转地址参数无效，或当前登录任务尚未结束')
                    login_relay.bind(body['origin'])
                    result = login_recovery.state()
                elif action in ('login-relay-provision', 'login-relay-check', 'login-relay-phone'):
                    if body or login_recovery.busy():
                        raise ValueError('请先结束当前登录任务；此操作不接受额外内容')
                    if action == 'login-relay-provision':
                        login_relay.provision()
                        result = login_recovery.state()
                    elif action == 'login-relay-check':
                        result = login_recovery.check_relay()
                    else:
                        result = {**login_relay.phone_configuration(), 'account': login_recovery.config()['account']}
                else:
                    raise ValueError('登录恢复操作不存在')
            elif action in ('asset-reference-propose','asset-reference-review'):
                import asset_references
                result = asset_references.propose(body,mode) if action=='asset-reference-propose' else asset_references.review(body,mode)
            elif action in ('asset-keyword-propose','asset-keyword-review'):
                import asset_keywords
                result = asset_keywords.propose(body,mode) if action=='asset-keyword-propose' else asset_keywords.review(body,mode)
            elif action in ('discovery-save','discovery-author'):
                import discovery_tracking
                result = discovery_tracking.save(body,mode) if action=='discovery-save' else discovery_tracking.author_command(body,mode)
            elif action == 'dm-test-check':
                result = messaging_http.state(clubops.DATA_DIR, mode)
            elif action == 'dm-test-send':
                raise ValueError('CO-DM-01 已完成网页测试，旧入口不再发送；新测试请另建草稿')
            elif action == 'uid-http-check':
                result = uid_messaging.state(mode)
            elif action == 'intent-outreach-control':
                result = intent_outreach.control(body.get('enabled'), mode)
            elif action == 'uid-inbox-read':
                import uid_inbox_store
                result = uid_inbox_store.read(body,mode)
            elif action == 'uid-inbox-link-reply':
                import uid_inbox_store
                result = uid_inbox_store.link_reply(body,mode)
            elif action == 'uid-inbox-sync-save':
                result = uid_inbox_sync.save(body,mode)
            elif action in ('uid-inbox-sync-start','uid-inbox-sync-stop'):
                result = uid_inbox_sync.control(body,action=='uid-inbox-sync-start',mode)
            elif action == 'uid-http-probe':
                if body:
                    raise ValueError('身份核对不接收 UID、凭证或消息参数；只使用本地配置')
                result = uid_messaging.probe_identity(mode)
            elif action == 'semantic-analyze':
                result = semantic.analyze_one(body, mode)
            elif action == 'semantic-save':
                result = semantic.save(body, mode)
            elif action == 'semantic-queue-cancel':
                if body:
                    raise ValueError('停止队列不接受额外参数')
                result = semantic_queue.cancel_all(mode)
            elif action == 'uid-http-target':
                result = uid_messaging.register_target(body, mode)
            elif action == 'live-save':
                result = live_monitor.save(body, mode)
            elif action == 'live-start':
                result = live_monitor.start(body, mode)
            elif action == 'live-stop':
                result = live_monitor.stop(body.get('id'), mode)
            elif action == 'live-track-start':
                result = live_tracking.start(body, mode)
            elif action == 'live-track-stop':
                result = live_tracking.stop(body.get('id'), mode)
            elif action == 'live-discover':
                import live_discovery
                result = live_discovery.discover(mode)
            elif action == 'live-room-toggle':
                import live_room_pool
                result = live_room_pool.toggle(body, mode)
            elif action == 'live-review':
                result = live_workflow.review(body, mode)
            elif action == 'uid-http-send':
                if set(body) != {'id'} or type(body['id']) is not int or body['id'] < 1:
                    raise ValueError('仅接受已保存草稿的任务 ID')
                result = uid_messaging.send_one(int(body['id']), mode)
            elif action == 'collector-start':
                result = collector.start(body, mode)
            elif action == 'monitor-save':
                result = monitoring.save(body, mode)
            elif action in ('monitor-start', 'monitor-stop'):
                result = monitoring.command(action.removeprefix('monitor-'), mode)
            elif action == 'collector-restart':
                result = collector.resume(body['id'], body.get('request_id'), mode)
            elif action == 'collection-plan-save':
                result = collection_scheduler.save(body, mode)
            elif action in ('collection-plan-start', 'collection-plan-pause'):
                result = collection_scheduler.command(body['id'], action.removeprefix('collection-plan-'), mode)
            elif action in ('collector-cancel', 'collector-resume'):
                result = collector.command(body['id'], action.removeprefix('collector-'), mode)
            else:
                result = clubops.mutate(action, body, mode)
            self.respond({'ok': True, 'result': result})
        except (ValueError, KeyError, TypeError) as exc:
            self.respond({'error': str(exc)}, 400)
        except Exception:
            if urlparse(self.path).path.startswith('/api/login-'):
                return self.respond({'error': '登录恢复操作未完成，请检查中转连接或配对状态'}, 500)
            if urlparse(self.path).path == '/api/uid-http-probe':
                return self.respond({'error': 'HTTP 身份核对未完成；此入口不创建会话或发送消息，请检查本机会话提供器'}, 500)
            if urlparse(self.path).path == '/api/uid-http-send':
                return self.respond({'error': 'HTTP 任务发生异常，提交结果未知；请核对会话与任务账本，不要重复发送'}, 500)
            self.respond({'error': '操作失败，数据未提交；请重试或检查服务日志'}, 500)
            import traceback
            traceback.print_exc()


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'

    def __init__(self, *args, **kwargs):
        self.lifecycle_lock = threading.RLock()
        self.instance_id = uuid.uuid4().hex
        self.stopping = False
        self.active_writes = 0
        super().__init__(*args, **kwargs)

    def service_state(self):
        return {'service': 'ClubOps', 'pid': os.getpid(), 'instance_id': self.instance_id,
                'status': 'stopping' if self.stopping else 'running', 'graceful_shutdown': True,
                'data_directory': str(clubops.DATA_DIR), 'python': sys.executable,
                'external_access': team_access.state()}

    def prepare_stop(self):
        # HTTP admissions share lifecycle_lock. Scheduler admissions share the
        # collector lock. Never interrupt a collector, live session or sender.
        with self.lifecycle_lock, collector.GUARD, live_monitor.GUARD, uid_inbox_sync.GUARD, intent_outreach.GUARD, clubops.LOCKS['live'], clubops.db() as c:
            active_plan = c.execute("SELECT 1 FROM collection_plans WHERE status='running' LIMIT 1").fetchone()
            active_model = c.execute("SELECT 1 FROM semantic_jobs WHERE status IN ('queued','running','cancelling') LIMIT 1").fetchone()
            if (self.active_writes != 1 or collector.ACTIVE or live_monitor.ACTIVE or
                    uid_messaging.GUARD.locked() or uid_inbox_sync.ACTIVE is not None or semantic.GUARD.locked() or active_model or active_plan or login_recovery.busy()):
                raise ValueError('仍有运行任务或写入；请先停止任务，服务未关闭')
            self.stopping = True
            collection_scheduler.STOP.set()
            live_tracking.STOP.set()
            uid_inbox_sync.STOP.set()
            intent_outreach.STOP.set()

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    # Bind before database initialization: a busy port must not migrate data.
    httpd = LocalHTTPServer((HOST, PORT), Handler)
    try:
        with runtime.data_lock(clubops.DATA_DIR):
            clubops.init()
            uid_messaging.recover()
            import uid_inbox_store
            uid_inbox_store.recover()
            uid_inbox_sync.recover()
            analysis_store.recover()
            semantic_queue.recover()
            live_monitor.recover()
            live_tracking.recover()
            collector.recover()
            login_recovery.recover()
            collection_scheduler.recover()
            collection_scheduler.start_service()
            semantic_queue.start_service()
            live_tracking.start_service()
            uid_inbox_sync.start_service()
            intent_outreach.start_service()
            team_access.start_service(httpd.server_address[1])
            print(f'ClubOps 已启动：http://{HOST}:{httpd.server_address[1]}/', flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                team_access.shutdown()
                uid_inbox_sync.shutdown()
                intent_outreach.shutdown()
                live_tracking.shutdown()
                live_monitor.shutdown()
                collection_scheduler.shutdown()
                login_recovery.shutdown()
                collector.shutdown()
                semantic_queue.shutdown()
    finally:
        httpd.server_close()


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f'启动失败：{exc}。未接管或停止其他服务。')
