"""Explicit read-account assignments and immutable per-batch account binding.

Business records and the work scheduler stay shared. Only the HTTP credential
directory changes in each child process; the sender profile is never replaced.
"""
from pathlib import Path
import json
import re
import clubops as app

SCHEMA='''CREATE TABLE IF NOT EXISTS collection_accounts (
 account_id TEXT PRIMARY KEY,sender_uid TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',
 storage TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,roles TEXT NOT NULL,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_task_accounts (
 task_id INTEGER PRIMARY KEY REFERENCES collection_tasks(id),account_id TEXT NOT NULL,
 sender_uid TEXT NOT NULL,storage TEXT NOT NULL,role TEXT NOT NULL,frozen_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_collection_account_tasks ON collection_task_accounts(account_id,task_id);
CREATE TABLE IF NOT EXISTS account_role_runs (
 id INTEGER PRIMARY KEY,run_key TEXT NOT NULL UNIQUE,account_id TEXT NOT NULL,
 sender_uid TEXT NOT NULL,storage TEXT NOT NULL,role TEXT NOT NULL,created_at TEXT NOT NULL);
'''
ROLES=('comments','discovery','live','groups','outreach')
ROLE_LABELS=dict(comments='评论采集',discovery='作品与作者发现',live='直播监控',groups='群聊监控与找群',outreach='私信引流')


def role_accounts(c,role):
    if role not in ROLES:raise ValueError('任务职责无效')
    return [dict(r) for r in c.execute('''SELECT * FROM collection_accounts
      WHERE enabled=1 AND EXISTS(SELECT 1 FROM json_each(roles) WHERE value=?)
      ORDER BY account_id''',(role,))]


def select_role(c,role,run_key):
    if role not in ROLES or not isinstance(run_key,str) or len(run_key)>150:raise ValueError('任务绑定参数无效')
    old=c.execute('SELECT * FROM account_role_runs WHERE run_key=?',(run_key,)).fetchone()
    if old:
        if old['role']!=role:raise ValueError('任务已绑定其他职责')
        return dict(old)
    rows=role_accounts(c,role)
    if not rows:
        if c.execute('SELECT 1 FROM collection_accounts').fetchone():raise ValueError('没有分配'+ROLE_LABELS[role]+'的账号')
        return None
    used={r['account_id']:r['last_id'] for r in c.execute('SELECT account_id,MAX(id) last_id FROM account_role_runs WHERE role=? GROUP BY account_id',(role,))}
    row=min(rows,key=lambda a:(used.get(a['account_id'],0),a['account_id']))
    if c.execute('SELECT 1 FROM account_login_jobs WHERE account_id=? AND finished_at IS NULL',(row['account_id'],)).fetchone():
        raise ValueError('该账号正在登录核对，请完成后再开始任务')
    c.execute('INSERT INTO account_role_runs(run_key,account_id,sender_uid,storage,role,created_at) VALUES(?,?,?,?,?,?)',
      (run_key,row['account_id'],row['sender_uid'],row['storage'],role,app.now()))
    return dict(row)


def directory(binding):
    if not binding or binding['storage']=='primary':return Path(app.DATA_DIR).resolve()
    account=binding['account_id']
    if binding['storage']!='isolated' or not re.fullmatch(r'[0-9A-Za-z_.-]{1,32}',account):
        raise ValueError('采集账号目录无效')
    root=Path(app.DATA_DIR).resolve()
    target=(root/'collection-accounts'/account).resolve()
    if not target.is_relative_to(root/'collection-accounts'):
        raise ValueError('采集账号目录超出工作区')
    return target


def state(mode='live'):
    import collector_http_session as sessions
    import uid_session
    with app.db(mode) as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM collection_accounts ORDER BY account_id')]
        for row in rows:
            row['group_count']=c.execute('SELECT COUNT(*) FROM monitored_groups WHERE account_uid=? AND member=1',(row['sender_uid'],)).fetchone()[0]
            latest=c.execute('''SELECT t.id,t.status,t.updated_at FROM collection_tasks t JOIN collection_task_accounts a ON a.task_id=t.id
              WHERE a.account_id=? ORDER BY t.id DESC LIMIT 1''',(row['account_id'],)).fetchone()
            row['latest_collection']=dict(latest) if latest else None
    for row in rows:
        row['roles']=json.loads(row['roles']);row['enabled']=bool(row['enabled'])
        status=sessions.status(directory(row)) if mode=='live' else {}
        row['session']={k:status.get(k) for k in ('ready','status','account','expires_at')}
        im=uid_session.local_status(directory(row)) if mode=='live' else {}
        row['im_session']={k:im.get(k) for k in ('im_read_verified','checked_at')}
        if row['im_session'].get('im_read_verified'):
            try:
                saved=uid_session.load(directory(row)/'private/uid-http/session.dpapi')
                row['im_session']['im_read_verified']=bool(saved.get('im_verified') and saved['account']==row['account_id'] and saved['sender_uid']==row['sender_uid'])
            except (OSError,ValueError):row['im_session']['im_read_verified']=False
    return dict(accounts=rows,roles=[dict(id=k,label=ROLE_LABELS[k]) for k in ROLES],distribution='shared_queue_round_robin')


def save(body,mode='live'):
    if mode!='live' or set(body)!={'account_id','sender_uid','storage','roles','enabled','label'}:
        raise ValueError('采集账号参数无效')
    account=body['account_id'];uid=body['sender_uid'];roles=body['roles']
    if not isinstance(account,str) or not re.fullmatch(r'[0-9A-Za-z_.-]{1,32}',account):raise ValueError('抖音号无效')
    if not isinstance(uid,str) or not re.fullmatch(r'\d{1,22}',uid):raise ValueError('账号 UID 无效')
    if type(body['enabled']) is not bool or body['storage'] not in ('primary','isolated'):raise ValueError('账号配置无效')
    if not isinstance(roles,list) or any(r not in ROLES for r in roles) or len(roles)!=len(set(roles)):raise ValueError('采集职责无效')
    if not isinstance(body['label'],str) or len(body['label'])>80:raise ValueError('账号名称过长')
    if body['enabled'] and not roles:raise ValueError('启用的采集账号须选择职责')
    import collector_http_session as sessions
    import uid_session
    with app.LOCKS[mode],app.db(mode) as c:
        existing=c.execute('SELECT * FROM collection_accounts WHERE account_id=?',(account,)).fetchone()
        if existing and (existing['sender_uid']!=uid or existing['storage']!=body['storage']):raise ValueError('已记录账号的身份或目录不可替换')
        previous=set(json.loads(existing['roles'])) if existing and existing['enabled'] else set()
        added=set(roles)-previous if body['enabled'] else set()
        # Renaming or removing assignments must also work when a session expires.
        if not existing or added.intersection({'comments','discovery','live'}):
            session=sessions.load(directory(body))
            if session['account']!=account or session['sender_uid']!=uid:raise ValueError('独立保存的登录身份与所选账号不一致')
        if added.intersection({'groups','outreach'}):
            vault=directory(body)/'private/uid-http/session.dpapi'
            session=uid_session.load(vault)
            if session['account']!=account or session['sender_uid']!=uid or session.get('im_verified') is not True:
                raise ValueError('请先核对该账号的私信与群聊登录身份')
        other=c.execute("SELECT account_id FROM collection_accounts WHERE storage='primary' AND account_id<>?",(account,)).fetchone()
        if body['storage']=='primary' and other:raise ValueError('主登录环境已绑定其他账号')
        c.execute('''INSERT INTO collection_accounts VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id)
          DO UPDATE SET label=excluded.label,enabled=excluded.enabled,roles=excluded.roles,updated_at=excluded.updated_at''',
          (account,uid,body['label'],body['storage'],int(body['enabled']),json.dumps(roles),app.now()))
        import group_accounts
        group_accounts.reconcile(c)
        app.event(c,'collection_accounts',f'更新采集账号 {account} 的职责；正在执行的批次保持原账号')
    return state(mode)


def prepared(mode='live'):
    """List only identities verified in our own saved vaults, never credentials."""
    import collector_http_session as sessions
    if mode!='live':return []
    with app.db(mode) as c:registered={r[0] for r in c.execute('SELECT account_id FROM collection_accounts')}
    roots=[dict(storage='primary',account_id='')]
    base=Path(app.DATA_DIR)/'collection-accounts'
    if base.is_dir():
        roots.extend(dict(storage='isolated',account_id=p.name) for p in base.iterdir()
          if p.is_dir() and not p.is_symlink() and re.fullmatch(r'[0-9A-Za-z_.-]{1,32}',p.name))
    result=[]
    for row in roots:
        try:
            session=sessions.load(directory(row))
            if row['storage']=='isolated' and row['account_id']!=session['account']:continue
            if session['account'] not in registered:
                result.append(dict(account_id=session['account'],sender_uid=session['sender_uid'],storage=row['storage']))
        except (OSError,ValueError):continue
    return result


def binding(c,task_id):
    row=c.execute('SELECT * FROM collection_task_accounts WHERE task_id=?',(task_id,)).fetchone()
    return dict(row) if row else None


def bind(c,task_id,kind,*,resume_from=None):
    old=binding(c,task_id)
    if old:return old
    role='discovery' if kind in ('search','author') else 'comments'
    chosen=binding(c,resume_from) if resume_from is not None else None
    if resume_from is not None and not chosen:
        # Legacy unbound tasks ran in the main profile, never inherit a newly
        # selected isolated account halfway through their reply checkpoints.
        chosen=c.execute("SELECT * FROM collection_accounts WHERE storage='primary'").fetchone()
    if resume_from is None:
        # One global scheduler assigns each batch once. Enabled accounts with
        # the same duty alternate batches, without duplicating video work.
        chosen=c.execute('''SELECT a.* FROM collection_accounts a
          WHERE a.enabled=1 AND EXISTS (SELECT 1 FROM json_each(a.roles) WHERE value=?)
          ORDER BY COALESCE((SELECT MAX(t.task_id) FROM collection_task_accounts t
            WHERE t.account_id=a.account_id AND t.role=?),0),a.account_id LIMIT 1''',(role,role)).fetchone()
        if not chosen and c.execute('SELECT 1 FROM collection_accounts').fetchone():
            raise ValueError('没有分配此职责的采集账号，请先配置账号任务')
    if chosen:
        if c.execute('SELECT 1 FROM account_login_jobs WHERE account_id=? AND finished_at IS NULL',(chosen['account_id'],)).fetchone():
            raise ValueError('该账号正在登录核对，请完成或取消登录后再采集')
        c.execute('INSERT INTO collection_task_accounts VALUES(?,?,?,?,?,?)',
            (task_id,chosen['account_id'],chosen['sender_uid'],chosen['storage'],role,app.now()))
    return binding(c,task_id)


def for_task(c,task_id):
    row=binding(c,task_id)
    if row:directory(row)
    return row
