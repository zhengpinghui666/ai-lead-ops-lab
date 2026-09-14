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
'''
ROLES=('comments','discovery')


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
    with app.db(mode) as c:rows=[dict(r) for r in c.execute('SELECT * FROM collection_accounts ORDER BY account_id')]
    for row in rows:
        row['roles']=json.loads(row['roles']);row['enabled']=bool(row['enabled'])
        status=sessions.status(directory(row)) if mode=='live' else {}
        row['session']={k:status.get(k) for k in ('status','account','expires_at','identity_status')}
    return dict(accounts=rows,roles=list(ROLES),distribution='shared_queue_round_robin')


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
    session=sessions.load(directory(body))
    if session['account']!=account or session['sender_uid']!=uid:raise ValueError('独立保存的登录身份与所选账号不一致')
    with app.LOCKS[mode],app.db(mode) as c:
        existing=c.execute('SELECT * FROM collection_accounts WHERE account_id=?',(account,)).fetchone()
        if existing and (existing['sender_uid']!=uid or existing['storage']!=body['storage']):raise ValueError('已记录账号的身份或目录不可替换')
        other=c.execute("SELECT account_id FROM collection_accounts WHERE storage='primary' AND account_id<>?",(account,)).fetchone()
        if body['storage']=='primary' and other:raise ValueError('主登录环境已绑定其他账号')
        c.execute('''INSERT INTO collection_accounts VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id)
          DO UPDATE SET label=excluded.label,enabled=excluded.enabled,roles=excluded.roles,updated_at=excluded.updated_at''',
          (account,uid,body['label'],body['storage'],int(body['enabled']),json.dumps(roles),app.now()))
        app.event(c,'collection_accounts',f'更新采集账号 {account} 的职责；正在执行的批次保持原账号')
    return state(mode)


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
        c.execute('INSERT INTO collection_task_accounts VALUES(?,?,?,?,?,?)',
            (task_id,chosen['account_id'],chosen['sender_uid'],chosen['storage'],role,app.now()))
    return binding(c,task_id)


def for_task(c,task_id):
    row=binding(c,task_id)
    if row:directory(row)
    return row
