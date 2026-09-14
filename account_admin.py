"""Local account management; registration never starts collection or messages."""
import hashlib
import json
import re
import threading
import subprocess
import os
import uuid
import clubops as app
import collection_accounts as accounts

SCHEMA='''CREATE TABLE IF NOT EXISTS account_login_jobs (
 id TEXT PRIMARY KEY,account_id TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,finished_at TEXT);
'''
GUARD=threading.RLock()
ACTIVE={}
DETAILS=dict(opening_browser='正在打开该账号的登录环境',manual_required='请在该账号窗口完成登录，然后核对身份',
 checking_identity='正在独立核对账号身份',completed='账号身份已核对，采集登录已保存',
 account_mismatch='登录账号不符，未替换已保存身份',cancelled='登录任务已取消',timeout='登录等待超时',
 browser_failed='登录任务未完成，已有账号记录保留',identity_failed='未通过独立身份核对',
 interrupted='服务重启结束了上次登录任务',preparing_im='正在核对该账号的私信与群聊会话')

def revision(row):
    return hashlib.sha256(json.dumps({k:row[k] for k in ('account_id','sender_uid','storage','roles','enabled','label')},sort_keys=True).encode()).hexdigest()[:20]

def _row(account,c):
    row=c.execute('SELECT * FROM collection_accounts WHERE account_id=?',(account,)).fetchone()
    if not row:raise ValueError('账号不存在')
    return dict(row)

def state(mode='live'):
    result=accounts.state(mode)
    with app.db(mode) as c:
        for row in result['accounts']:
            row['revision']=revision(_row(row['account_id'],c))
            job=c.execute('SELECT * FROM account_login_jobs WHERE account_id=? ORDER BY created_at DESC,id DESC LIMIT 1',(row['account_id'],)).fetchone()
            row['login_job']=dict(job) if job else None
    import uid_messaging
    settings,_=uid_messaging.config()
    result['sender_uid']=settings.get('sender_uid')
    result['prepared']=accounts.prepared(mode)
    return result

def create(body,mode='live'):
    if mode!='live' or set(body)!={'account_id','label'}:raise ValueError('新增账号参数无效')
    account=body['account_id'];label=body['label']
    if not isinstance(account,str) or not re.fullmatch(r'[0-9A-Za-z_.-]{2,32}',account):raise ValueError('请输入正确的抖音号')
    if not isinstance(label,str) or len(label)>80:raise ValueError('账号备注过长')
    with app.LOCKS[mode],app.db(mode) as c:
        if c.execute('SELECT 1 FROM collection_accounts WHERE account_id=?',(account,)).fetchone():raise ValueError('该账号已存在')
        c.execute('INSERT INTO collection_accounts VALUES(?,?,?,?,?,?,?)',(account,'',label,'isolated',0,'[]',app.now()))
        app.event(c,'account_create','新增账号 '+account+'；等待登录，尚未分配任务')
    return state(mode)

def save(body,mode='live'):
    if mode!='live' or set(body)!={'account_id','label','roles','enabled','revision'}:raise ValueError('账号分工参数无效')
    if not isinstance(body['roles'],list) or any(r not in accounts.ROLES for r in body['roles']):raise ValueError('账号职责无效')
    with app.LOCKS[mode]:
        with app.db(mode) as c:
            row=_row(body['account_id'],c)
        if revision(row)!=body['revision']:raise ValueError('账号分工已更新，请刷新后再保存')
        if not row['sender_uid']:raise ValueError('请先登录并核对该账号身份')
        if 'outreach' in body['roles']:
            import uid_messaging
            settings,_=uid_messaging.config()
            if settings.get('sender_uid')!=row['sender_uid']:raise ValueError('该账号尚未设为私信发送账号；请先完成私信通道切换')
        accounts.save({k:(body[k] if k in body else row[k]) for k in ('account_id','sender_uid','storage','roles','enabled','label')},mode)
    return state(mode)

def _phase(job,status):
    if status not in DETAILS:return
    with app.LOCKS['live'],app.db() as c:
        c.execute('UPDATE account_login_jobs SET status=?,detail=?,updated_at=? WHERE id=?',(status,DETAILS[status],app.now(),job))

def start_login(body,mode='live'):
    if mode!='live' or set(body)!={'account_id'}:raise ValueError('账号登录参数无效')
    import collector,login_recovery
    with GUARD,app.LOCKS[mode],app.db(mode) as c:
        row=_row(body['account_id'],c)
        if row['storage']=='primary':raise ValueError('请使用主账号既有的登录与手机设置入口')
        if ACTIVE or login_recovery.busy():raise ValueError('已有登录任务，请先完成或取消')
        running=c.execute('''SELECT 1 FROM collection_tasks t JOIN collection_task_accounts a ON a.task_id=t.id
          WHERE a.account_id=? AND t.finished_at IS NULL''',(row['account_id'],)).fetchone()
        if running:raise ValueError('该账号仍有在途采集，请等待本批结束')
        job=uuid.uuid4().hex;control=dict(process=None,cancel=False,account=row)
        c.execute('INSERT INTO account_login_jobs VALUES(?,?,?,?,?,?,NULL)',(job,row['account_id'],'opening_browser',DETAILS['opening_browser'],app.now(),app.now()))
        ACTIVE[job]=control
    thread=threading.Thread(target=_worker,args=(job,control),daemon=True,name='account-login-'+row['account_id'])
    control['thread']=thread;thread.start()
    return dict(id=job,account_id=row['account_id'])

def command(body,mode='live'):
    if mode!='live' or set(body)!={'id','command'} or body['command'] not in ('complete','cancel'):raise ValueError('登录操作无效')
    with GUARD:
        control=ACTIVE.get(body['id'])
        if not control:raise ValueError('该登录任务已结束，请刷新状态')
        if body['command']=='cancel':control['cancel']=True
        process=control.get('process')
        if process and process.poll() is None:
            process.stdin.write(json.dumps(dict(command=body['command']))+'\n');process.stdin.flush()
    return dict(id=body['id'])

def _worker(job,control):
    import collector,collector_http_session,sys
    row=control['account'];process=None;watchdog=None;status='browser_failed'
    try:
        node,package=collector.dependencies()
        if not node or not package.is_dir():return
        env={**os.environ,'CLUBOPS_DATA_DIR':str(accounts.directory(row)),'CLUBOPS_PLAYWRIGHT':str(package),
          'CLUBOPS_PYTHON':sys.executable,'PYTHONIOENCODING':'utf-8'}
        process=subprocess.Popen([node,str(collector.BASE/'scripts/account-login.cjs')],cwd=collector.BASE,env=env,
          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def expire():
            control['expired']=True
            if process.poll() is None:process.kill()
        watchdog=threading.Timer(720,expire);watchdog.daemon=True;watchdog.start()
        with GUARD:
            control['process']=process
            process.stdin.write(json.dumps(dict(command='start',account=row['account_id']))+'\n')
            if control['cancel']:process.stdin.write('{"command":"cancel"}\n')
            process.stdin.flush()
        for line in process.stdout:
            if len(line)>8192:break
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('type')=='status':_phase(job,event.get('status'))
            if event.get('type')=='result':
                status=event.get('status') if event.get('status') in DETAILS else 'identity_failed'
                if status=='completed':
                    session=collector_http_session.load(accounts.directory(row))
                    if session['account']!=row['account_id'] or row['sender_uid'] and session['sender_uid']!=row['sender_uid']:
                        status='account_mismatch'
                    else:
                        with app.LOCKS['live'],app.db() as c:
                            c.execute('UPDATE collection_accounts SET sender_uid=?,updated_at=? WHERE account_id=? AND sender_uid IN (?,?)',
                              (session['sender_uid'],app.now(),row['account_id'],'',session['sender_uid']))
        process.wait(timeout=5)
    except Exception:status='browser_failed'
    finally:
        if watchdog:watchdog.cancel()
        if process and process.poll() is None:process.kill()
        if process:
            process.wait(timeout=10)
            for stream in (process.stdin,process.stdout):
                if stream:stream.close()
        if control.get('expired'):status='timeout'
        if control['cancel']:status='cancelled'
        _phase(job,status)
        with app.LOCKS['live'],app.db() as c:c.execute('UPDATE account_login_jobs SET finished_at=? WHERE id=?',(app.now(),job))
        with GUARD:ACTIVE.pop(job,None)

def recover():
    with app.LOCKS['live'],app.db() as c:
        c.execute("UPDATE account_login_jobs SET status='interrupted',detail=?,finished_at=?,updated_at=? WHERE finished_at IS NULL",(DETAILS['interrupted'],app.now(),app.now()))

def shutdown():
    with GUARD:jobs=list(ACTIVE.items())
    for job,control in jobs:
        try:command(dict(id=job,command='cancel'))
        except (ValueError,OSError):pass
    for job,control in jobs:
        thread=control.get('thread')
        if thread:thread.join(timeout=10)
        process=control.get('process')
        if thread and thread.is_alive() and process and process.poll() is None:
            process.kill();thread.join(timeout=10)
