"""User-owned iPhone alerts. Keys stay in Windows DPAPI; alerts contain no customer data."""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import threading
import time
from urllib.parse import urlsplit, unquote
import uuid
from uid_session import crypt

MAGIC=b'CLUBOPS-BARK-1\n'
LOCK=threading.Lock()


def directory(data_dir):
    return Path(data_dir)/'private/phone-notify'


def parse_key(value):
    if not isinstance(value,str) or len(value)>2048:raise ValueError('请粘贴 Bark 首页的推送地址')
    try:
        url=urlsplit(value.strip())
        if url.scheme!='https' or url.hostname!='api.day.app' or url.port not in (None,443) or url.username or url.password or url.fragment:raise ValueError()
        key=unquote(url.path.strip('/').split('/')[0])
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',key):raise ValueError()
        return key
    except ValueError:raise ValueError('请使用 Bark 默认服务器 https://api.day.app/ 开头的完整推送地址') from None


def load(data_dir):
    path=directory(data_dir)/'bark.dpapi'
    if path.is_symlink():raise ValueError('手机通知配置无效')
    with path.open('rb') as stream:raw=stream.read(16385)
    if len(raw)>16384 or not raw.startswith(MAGIC):raise ValueError('手机通知配置无效')
    value=json.loads(crypt(raw[len(MAGIC):],decrypt=True))
    if not isinstance(value,dict) or set(value)!={'key','enabled','revision'} or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',value.get('key','')) or type(value['enabled']) is not bool or not re.fullmatch(r'[a-f0-9]{32}',value['revision']):raise ValueError('手机通知配置无效')
    return value


def connect(data_dir):
    root=directory(data_dir);root.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(root/'deliveries.db',timeout=3);c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY,kind TEXT NOT NULL,revision TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,confirmed_at REAL);''')
    return c


def state(data_dir):
    try:settings=load(data_dir)
    except FileNotFoundError:return {'configured':False,'enabled':False,'verified':False,'last_test':None}
    except (OSError,ValueError,TypeError):return {'configured':False,'enabled':False,'verified':False,'last_test':None,'error':'配置无法读取，请重新保存'}
    with closing(connect(data_dir)) as c:
        test=c.execute("SELECT id,status,created_at,confirmed_at FROM notices WHERE revision=? AND kind='test' ORDER BY created_at DESC LIMIT 1",(settings['revision'],)).fetchone()
        verified=c.execute("SELECT 1 FROM notices WHERE revision=? AND kind='test' AND confirmed_at IS NOT NULL LIMIT 1",(settings['revision'],)).fetchone()
        last=c.execute("SELECT status,created_at FROM notices WHERE revision=? AND kind='incident' ORDER BY created_at DESC LIMIT 1",(settings['revision'],)).fetchone()
    return {'configured':True,'enabled':settings['enabled'],'verified':bool(verified),'last_test':dict(test) if test else None,'last_alert':dict(last) if last else None}


def save(data_dir,body):
    if set(body)!={'url','enabled'} or type(body['enabled']) is not bool:raise ValueError('手机通知设置参数无效')
    previous=None
    try:previous=load(data_dir)
    except (FileNotFoundError,ValueError):pass
    key=parse_key(body['url']) if body['url'] else previous['key'] if previous else None
    if not key:raise ValueError('请先填写 Bark 推送地址')
    value={'key':key,'enabled':body['enabled'],'revision':previous['revision'] if previous and previous['key']==key else uuid.uuid4().hex}
    root=directory(data_dir);root.mkdir(parents=True,exist_ok=True)
    target=root/'bark.dpapi';temp=root/('.bark-'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as stream:stream.write(MAGIC+crypt(json.dumps(value).encode()));stream.flush();os.fsync(stream.fileno())
        os.replace(temp,target)
    finally:temp.unlink(missing_ok=True)
    return state(data_dir)


def send(key,title,body):
    """Bark acceptance is not a delivery receipt. No redirect or automatic resend."""
    payload={'device_key':key,'title':title,'body':body,'group':'ClubOps','level':'timeSensitive','isArchive':0}
    conn=http.client.HTTPSConnection('api.day.app',443,timeout=5,context=ssl.create_default_context())
    try:
        conn.request('POST','/push',json.dumps(payload,ensure_ascii=False).encode('utf-8'),{'Content-Type':'application/json; charset=utf-8'})
        reply=conn.getresponse();raw=reply.read(4097)
        if len(raw)>4096:return 'unknown'
        try:value=json.loads(raw)
        except (ValueError,UnicodeError):return 'unknown'
        return 'accepted' if reply.status==200 and isinstance(value,dict) and value.get('code')==200 else 'rejected'
    except Exception:return 'unknown'
    finally:conn.close()


def deliver(data_dir,settings,notice_id,kind,title,text,transport=send):
    # Reserve before I/O: ambiguous/crashed requests are visible and never silently replayed.
    with closing(connect(data_dir)) as c,c:
        inserted=c.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?,?,?,NULL)',(notice_id,kind,settings['revision'],'unknown',time.time())).rowcount
    if not inserted:return None
    try:result=transport(settings['key'],title,text)
    except Exception:result='unknown'
    if result not in ('accepted','rejected','unknown'):result='unknown'
    with closing(connect(data_dir)) as c,c:c.execute('UPDATE notices SET status=? WHERE id=?',(result,notice_id))
    return result


def test(data_dir,body,transport=send):
    if body:raise ValueError('测试不接受额外参数')
    settings=load(data_dir)
    with closing(connect(data_dir)) as c:
        last=c.execute("SELECT created_at FROM notices WHERE kind='test' ORDER BY created_at DESC LIMIT 1").fetchone()
    if last and time.time()-last['created_at']<20:raise ValueError('请等 20 秒再发送测试，避免重复提醒')
    deliver(data_dir,settings,uuid.uuid4().hex,'test','ClubOps 锁屏通知测试','这是一条合成测试。看到通知后，请回电脑点击“我已收到”。没有请求抖音验证码，也没有更改采集。',transport)
    return state(data_dir)


def confirm(data_dir,body):
    if set(body)!={'id'} or not isinstance(body['id'],str):raise ValueError('请选择本次通知测试')
    settings=load(data_dir)
    with closing(connect(data_dir)) as c,c:
        row=c.execute("SELECT * FROM notices WHERE id=? AND revision=? AND kind='test'",(body['id'],settings['revision'])).fetchone()
        if not row or row['status']=='rejected' or time.time()-row['created_at']>1800:raise ValueError('本次测试已失效，请重新测试')
        c.execute('UPDATE notices SET confirmed_at=? WHERE id=?',(time.time(),body['id']))
    return state(data_dir)


def notify_incidents(bridge,transport=send):
    data_dir=bridge.data_dir
    current=state(data_dir)
    if not current['enabled'] or not current['verified']:return
    settings=load(data_dir)
    with closing(bridge.connect()) as c:
        rows=c.execute("SELECT id,channel FROM incidents WHERE active=1 AND state='needs_user' AND channel<>'self_test' ORDER BY detected_at").fetchall()
    names={'comments':'评论监控','live':'直播监控','groups':'群聊监控','inbox':'收件同步','service':'本机服务','health':'运行检查'}
    for row in rows:
        if row['channel'] not in names:continue
        deliver(data_dir,settings,row['id']+':'+settings['revision'],'incident','ClubOps 需要你处理',names[row['channel']]+'遇到需要人工处理的问题。请查看 ClubOps 工作台中的异常详情；同一故障不会重复提醒。',transport)


def schedule(bridge):
    # The phone network must never hold up Codex fault delivery or its heartbeat.
    if not LOCK.acquire(blocking=False):return
    def worker():
        try:notify_incidents(bridge)
        except Exception:pass
        finally:LOCK.release()
    threading.Thread(target=worker,name='phone-incident-notify',daemon=True).start()
