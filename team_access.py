"""Outbound-only connection to the user's public workbench entry."""
import base64
import gzip
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import clubops as app
from uid_session import crypt

MAGIC=b'CLUBOPS-TEAM-ACCESS-1\n'
GETS={'/','/app.js','/app.css','/vendor/lucide.min.js','/login','/login.js','/login.css','/login-guide','/iphone-script',
      '/api/state','/api/collector','/api/monitor-comments','/api/login-recovery','/api/live-message','/api/live-history','/api/analysis-history','/api/lead-live-history','/api/collector-evidence','/api/export'}
STOP=threading.Event()
THREAD=None
SOCKET=None
STATUS={'enabled':False,'connected':False}

def config_path():return app.DATA_DIR/'private'/'team-access.dpapi'

def save(value):
    u=urlsplit(value['origin'])
    if u.scheme!='https' or not re.fullmatch(r'clubops-team-[a-f0-9]{6}\.pages\.dev',u.netloc) or u.path or u.query or u.fragment:raise ValueError('外网入口格式无效')
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,128}',value['token']):raise ValueError('电脑连接凭证格式无效')
    if type(value['enabled']) is not bool:raise ValueError('开关无效')
    dest=config_path();dest.parent.mkdir(parents=True,exist_ok=True);temp=dest.with_suffix('.tmp')
    temp.write_bytes(MAGIC+crypt(json.dumps(value).encode()));os.replace(temp,dest)

def load():
    raw=config_path().read_bytes()
    if len(raw)>16384 or not raw.startswith(MAGIC):raise ValueError('连接配置无效')
    return json.loads(crypt(raw[len(MAGIC):],decrypt=True))

def state():return dict(STATUS)

def allowed(method,path):
    if not isinstance(path,str):return False
    u=urlsplit(path)
    if not path.startswith('/') or path.startswith('//') or u.scheme or u.netloc or u.fragment or len(path)>4200 or '\\' in path:return False
    return (method=='GET' and u.path in GETS or method=='POST' and re.fullmatch(r'/api/[a-z][a-z-]{0,64}',u.path) is not None and u.path!='/api/service-stop')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def forward(value,origin):
    """One request, one execution. Never follows redirects or retries a write."""
    method,path=value.get('method'),value.get('path','')
    if not allowed(method,path):raise ValueError('route')
    content=value.get('body','')
    if not isinstance(content,str) or len(content.encode())>262144:raise ValueError('size')
    source=value.get('headers',{});headers={'Origin':origin}
    for key in ('content-type','x-clubops-token'):
        text=source.get(key,'')
        if not isinstance(text,str) or len(text)>4096 or '\r' in text or '\n' in text:raise ValueError('header')
        if text:headers[key]=text
    request=urllib.request.Request(origin+path,data=content.encode() if method=='POST' else None,method=method,headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:response=opener.open(request,timeout=16)
    except urllib.error.HTTPError as error:response=error
    with response:
        raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('response_size')
        encoded=raw
        if len(raw)>=2048:
            compressed=gzip.compress(raw,compresslevel=5,mtime=0)
            if len(compressed)<len(raw):encoded=compressed
        result={'id':value['id'],'status':response.status,'content_type':response.headers.get('Content-Type','application/octet-stream'),'body':base64.b64encode(encoded).decode()}
        if encoded is not raw:result['body_encoding']='gzip'
        return result

def start_service(port):
    global THREAD
    if THREAD and THREAD.is_alive():return
    try:config=load()
    except (OSError,ValueError,KeyError):return
    if not config.get('enabled'):return
    origin='http://127.0.0.1:'+str(int(port));STOP.clear();STATUS.update(enabled=True,connected=False,origin=config['origin'])
    def loop():
        global SOCKET
        try:
            # Project-local pure-Python dependency, separate from the host runtime.
            dependency=Path(__file__).resolve().parent/'.tools/team-access-libs'
            if dependency.is_dir() and str(dependency) not in sys.path:sys.path.insert(0,str(dependency))
            import websocket
        except ImportError:STATUS.update(connected=False,error='dependency_missing');return
        backoff=2
        while not STOP.is_set():
            pool=ThreadPoolExecutor(max_workers=6,thread_name_prefix='team-access');capacity=threading.BoundedSemaphore(12)
            def opened(ws):STATUS.update(connected=True,error=None)
            def receive(ws,message):
                if STOP.is_set() or message=='pong':return
                try:
                    value=json.loads(message)
                    if not re.fullmatch(r'[a-f0-9-]{36}',value.get('id','')):return
                except (ValueError,TypeError):return
                if not capacity.acquire(blocking=False):return
                def work():
                    try:
                        try:result=forward(value,origin)
                        except Exception:result={'id':value['id'],'status':502,'content_type':'application/json; charset=utf-8','body':base64.b64encode(json.dumps({'error':'电脑响应未确认，请核对操作记录'}).encode()).decode()}
                        if not STOP.is_set():
                            try:ws.send(json.dumps(result))
                            except Exception:pass
                    finally:capacity.release()
                pool.submit(work)
            ws=websocket.WebSocketApp(config['origin'].replace('https://','wss://',1)+'/_access/connect',header={'Authorization':'Bearer '+config['token'],'User-Agent':'ClubOps-Connector/1.0'},
                on_open=opened,on_message=receive,on_error=lambda *_:STATUS.update(connected=False,error='connection_unavailable'),on_close=lambda *_:STATUS.update(connected=False))
            SOCKET=ws
            try:ws.run_forever(ping_interval=30,ping_timeout=10,suppress_origin=True)
            except Exception:STATUS.update(connected=False,error='connection_unavailable')
            finally:pool.shutdown(wait=True,cancel_futures=True)
            if STOP.wait(backoff):break
            backoff=min(30,backoff*2)
        STATUS.update(connected=False)
    THREAD=threading.Thread(target=loop,name='team-access',daemon=True);THREAD.start()

def shutdown():
    STOP.set()
    if SOCKET:
        try:SOCKET.close()
        except Exception:pass
    if THREAD:THREAD.join(timeout=20)
    STATUS.update(connected=False)
