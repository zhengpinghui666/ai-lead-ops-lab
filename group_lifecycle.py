"""Account-bound exits for groups restricted to administrator messages.

Command 652 and its three fields follow the locally observed official PCIM SDK.
An accepted command is not a completed exit: a fresh complete catalog must agree.
"""
import hashlib
import json
import re

import clubops as app
import uid_protocol as wire
import uid_session
import uid_transport
import uid_inbox

SCHEMA = '''
CREATE TABLE IF NOT EXISTS group_exits (
 account_uid TEXT NOT NULL, conversation_id TEXT NOT NULL,
 reason TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, proof TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(account_uid,conversation_id)
);
'''
POLICY = 'group_exit_admin_only'
REASON = '只有群主和管理员可以发消息，不适合作为需求采集群'


def leave_body(group):
    cid=group['conversation_id'];short=wire.numeric_uid(group['conversation_short_id'])
    if not re.fullmatch(r'[A-Za-z0-9:_-]{5,100}',cid):raise ValueError('群标识无效')
    return wire.field(652,wire.field(1,cid)+wire.field(2,int(short))+wire.field(3,2))


def validate_body(body):
    outer=wire.decode(body)
    if set(outer)!={652}:raise ValueError('仅允许本人退群')
    row=wire.decode(wire.one(outer,652,2))
    if set(row)!={1,2,3} or wire.one(row,3,0)!=2:raise ValueError('只允许退出群聊')
    group=dict(conversation_id=wire.text(row,1),conversation_short_id=str(wire.one(row,2,0)))
    if body!=leave_body(group):raise ValueError('退群参数无效')
    return group


def leave(account,group,*,provider=None,exchange=None):
    if group.get('account_uid')!=account or not group.get('member'):raise ValueError('当前账号未确认在群内')
    provider=provider or uid_session.Provider();exchange=exchange or uid_transport.request
    evidence=[];uid_inbox._identity(account,provider,exchange,evidence)
    body=leave_body(group)
    prepared=provider.prepare('group_leave',body,dict(sender_uid=account,command=652))
    seq=uid_transport.validate_envelope(prepared,652,body)
    status,mime,raw=exchange('group_leave',prepared)
    proof=dict(http_status=status,response_bytes=len(raw),response_sha256=hashlib.sha256(raw).hexdigest())
    if status!=200 or 'protobuf' not in mime.lower():raise ValueError('退群结果未确认')
    response=wire.decode(raw)
    if (wire.one(response,1,0)!=652 or wire.one(response,2,0)!=seq
            or wire.one(response,13,0)!=int(account) or wire.one(response,5,0,0)!=0):
        raise ValueError('退群回执身份或请求不匹配')
    proof['platform_code']=wire.one(response,3,0,0)
    if proof['platform_code']!=0:return dict(status='rejected',proof=proof)
    if wire.text(response,4)!='OK':raise ValueError('退群平台结果未确认')
    return dict(status='accepted',proof=proof)


def excluded(c,account,cid):
    return c.execute('SELECT 1 FROM group_exits WHERE account_uid=? AND conversation_id=?',(account,cid)).fetchone() is not None


def schedule(c,account,cid,evidence):
    c.execute('INSERT OR IGNORE INTO group_exits VALUES(?,?,?,?,?,?,?,?)',
              (account,cid,REASON,json.dumps(evidence,ensure_ascii=False),'queued',app.now(),app.now(),'{}'))
    c.execute("UPDATE monitored_groups SET enabled=0,status='leaving',detail=? WHERE account_uid=? AND conversation_id=?",
              (REASON+'；等待退群回执',account,cid))


def observe(c,account,rows,complete):
    setting=c.execute('SELECT value FROM settings WHERE key=?',(POLICY,)).fetchone()
    enabled=bool(setting and json.loads(setting[0]).get('enabled'))
    members={r['conversation_id']:r for r in rows}
    if enabled:
        for row in rows:
            if row.get('member') and row.get('admin_only') and not excluded(c,account,row['conversation_id']):
                schedule(c,account,row['conversation_id'],dict(source='platform_group_permissions',block_status=1,block_normal_only=True))
    for item in c.execute('SELECT * FROM group_exits WHERE account_uid=?',(account,)).fetchall():
        cid=item['conversation_id'];row=members.get(cid)
        gone=(row is not None and not row['member']) or (complete and row is None)
        # Even rejected / unknown submissions retain the exclusion. No blind resend.
        status='left' if gone else item['status']
        if status!=item['status']:
            c.execute('UPDATE group_exits SET status=?,updated_at=? WHERE account_uid=? AND conversation_id=?',(status,app.now(),account,cid))
        detail=REASON+('；已确认退出，不再自动加入' if status=='left' else '；退群处理中' if status=='queued' else '；退群回执待核对，不重复提交')
        c.execute('UPDATE monitored_groups SET enabled=0,status=?,detail=? WHERE account_uid=? AND conversation_id=?',
                  ('left' if status=='left' else 'leaving',detail,account,cid))


def process_one(account,*,leaver=None):
    """Caller owns the shared IM guard. Commit the attempt before network I/O."""
    with app.LOCKS['live'],app.db() as c:
        row=c.execute("SELECT g.* FROM group_exits e JOIN monitored_groups g USING(account_uid,conversation_id) WHERE e.account_uid=? AND e.status='queued' ORDER BY e.created_at LIMIT 1",(account,)).fetchone()
        if not row:return False
        group=dict(row)
        if not group['member']:
            c.execute("UPDATE group_exits SET status='left',updated_at=? WHERE account_uid=? AND conversation_id=?",(app.now(),account,group['conversation_id']))
            return True
        c.execute("UPDATE group_exits SET status='unknown',updated_at=? WHERE account_uid=? AND conversation_id=?",(app.now(),account,group['conversation_id']))
    try:result=(leaver or leave)(account,group)
    except Exception:result=dict(status='unknown',proof=dict(detail='提交结果未确认，需要读取群目录核对'))
    with app.LOCKS['live'],app.db() as c:
        c.execute('UPDATE group_exits SET status=?,proof=?,updated_at=? WHERE account_uid=? AND conversation_id=?',
                  (result['status'],json.dumps(result['proof'],ensure_ascii=False),app.now(),account,group['conversation_id']))
        c.execute('INSERT INTO group_reads(group_id,account_uid,operation,status,detail,created_at) VALUES(?,?,?,?,?,?)',
                  (group['id'],account,'leave',result['status'],json.dumps(result['proof']),app.now()))
    return True


def control(body,mode='live'):
    import group_monitor
    import uid_inbox_store
    if mode!='live' or body.get('admin_only') is not True:raise ValueError('退群范围必须为仅管理员发言的群')
    account=uid_inbox_store._account()
    if body.get('account_uid')!=account:raise ValueError('退群账号与当前账号不符')
    with group_monitor.GUARD,app.LOCKS[mode],app.db(mode) as c:
        cid=body.get('conversation_id')
        row=c.execute('SELECT * FROM monitored_groups WHERE account_uid=? AND conversation_id=?',(account,cid)).fetchone()
        if not row:raise ValueError('当前账号没有该群记录')
        if not excluded(c,account,cid):schedule(c,account,cid,dict(source='user_confirmed_admin_only'))
        if body.get('apply_to_similar') is True:
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(POLICY,json.dumps(dict(enabled=True,granted_at=app.now()))))
    return dict(account_uid=account,conversation_id=cid,status='queued')
