"""One observer per logical group; membership remains evidence owned by each account."""
from contextlib import contextmanager
import json
import clubops as app
import collection_accounts as accounts
import account_scope

SCHEMA='''CREATE TABLE IF NOT EXISTS group_account_assignments (
 conversation_id TEXT PRIMARY KEY,account_uid TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_group_assignment_account ON group_account_assignments(account_uid);
'''

def role_allowed(c,uid):
    if not c.execute('SELECT 1 FROM collection_accounts').fetchone():
        import uid_inbox_store
        return uid==uid_inbox_store._account()
    return any(row['sender_uid']==uid for row in accounts.role_accounts(c,'groups'))

def owner(c,cid,preferred=None):
    rows=accounts.role_accounts(c,'groups')
    if not rows:return None
    uids={r['sender_uid'] for r in rows}
    old=c.execute('SELECT account_uid FROM group_account_assignments WHERE conversation_id=?',(cid,)).fetchone()
    if old and old[0] in uids:return old[0]
    # Retain a working member before considering a new account that must join.
    members=[r[0] for r in c.execute('SELECT account_uid FROM monitored_groups WHERE conversation_id=? AND member=1 ORDER BY enabled DESC,id',(cid,)) if r[0] in uids]
    counts={r[0]:r[1] for r in c.execute('SELECT account_uid,COUNT(*) FROM group_account_assignments GROUP BY account_uid')}
    chosen=members[0] if members else min(uids,key=lambda uid:(counts.get(uid,0),uid!=preferred,uid))
    c.execute('INSERT OR REPLACE INTO group_account_assignments VALUES(?,?,?)',(cid,chosen,app.now()))
    return chosen

def owns(c,cid,uid):
    assigned=owner(c,cid,uid)
    return assigned==uid if assigned else role_allowed(c,uid)

def claim(c,cid,uid):
    if not role_allowed(c,uid):raise ValueError('该账号尚未分配群聊监控任务')
    c.execute('INSERT OR REPLACE INTO group_account_assignments VALUES(?,?,?)',(cid,uid,app.now()))
    c.execute("UPDATE monitored_groups SET enabled=0,status='assigned_elsewhere',detail='已分配给其他账号采集；保留群身份' WHERE conversation_id=? AND account_uid<>? AND enabled=1",(cid,uid))

def reconcile(c):
    if not c.execute('SELECT 1 FROM collection_accounts').fetchone():return
    # Existing memberships are never copied to another account.
    for row in c.execute('SELECT DISTINCT conversation_id FROM monitored_groups WHERE enabled=1').fetchall():
        owner(c,row[0])
    for row in c.execute('SELECT * FROM group_account_assignments').fetchall():
        uid=owner(c,row['conversation_id'])
        if not uid:continue
        was_active=c.execute('SELECT 1 FROM monitored_groups WHERE conversation_id=? AND enabled=1',(row['conversation_id'],)).fetchone()
        c.execute("UPDATE monitored_groups SET enabled=0,status='assigned_elsewhere',detail='已分配给其他账号采集；保留群身份' WHERE conversation_id=? AND account_uid<>? AND enabled=1",(row['conversation_id'],uid))
        if was_active:
            c.execute("UPDATE monitored_groups SET enabled=1,status='waiting',detail='按账号分工接手，等待读取',next_run_at=? WHERE conversation_id=? AND account_uid=? AND member=1 AND matched=1 AND status IN ('available','assigned_elsewhere')",(app.now(),row['conversation_id'],uid))

def sources_for(c,uid):
    """Prioritize public owners of assigned groups missing from this account."""
    reconcile(c)
    return [r[0] for r in c.execute('''SELECT DISTINCT p.owner_sec_uid FROM group_account_assignments a
      JOIN public_group_candidates p ON p.group_id=a.conversation_id
      WHERE a.account_uid=? AND p.matched=1 AND p.owner_sec_uid!=''
      AND NOT EXISTS(SELECT 1 FROM monitored_groups g WHERE g.account_uid=a.account_uid
        AND g.conversation_id=a.conversation_id AND g.member=1)
      AND NOT EXISTS(SELECT 1 FROM group_exits e WHERE e.account_uid=a.account_uid AND e.conversation_id=a.conversation_id)
      ORDER BY p.owner_sec_uid LIMIT 3''',(uid,))]

@contextmanager
def selected(account_id='',*,require_role=False):
    with app.db() as c:
        if account_id:
            row=c.execute('SELECT * FROM collection_accounts WHERE account_id=?',(account_id,)).fetchone()
            if not row or not row['sender_uid']:raise ValueError('请选择已核对身份的账号')
        else:
            rows=accounts.role_accounts(c,'groups')
            row=rows[0] if rows else c.execute("SELECT * FROM collection_accounts WHERE storage='primary'").fetchone()
        if require_role and row and not role_allowed(c,row['sender_uid']):
            raise ValueError('该账号尚未分配群聊监控任务')
    with account_scope.use(row):yield row

def state(mode='live',before=0,category_filter='all',account_id=''):
    import group_monitor
    if mode!='live':return group_monitor.state(mode,before,category_filter)
    with selected(account_id) as row:
        result=group_monitor.state(mode,before,category_filter)
    with app.db(mode) as c:
        choices=[dict(account_id=r['account_id'],label=r['label'],sender_uid=r['sender_uid'],
                      assigned=bool(r['enabled'] and 'groups' in json.loads(r['roles'])))
                 for r in c.execute('SELECT * FROM collection_accounts WHERE sender_uid!="" ORDER BY account_id')]
    return dict(result,account_id=row['account_id'] if row else '',accounts=choices)

def dispatch(action,body,mode):
    if mode!='live':raise ValueError('群账号操作仅用于正式工作区')
    import group_monitor,group_discovery,group_lifecycle
    if not isinstance(body,dict):raise ValueError('群账号参数无效')
    payload=dict(body);account=payload.pop('account_id','')
    if not isinstance(account,str):raise ValueError('群账号参数无效')
    mutating=action in ('group-discover','group-question','group-answer') or payload.get('enabled') is True
    with selected(account,require_role=mutating):
        if action=='group-discover':return group_monitor.refresh(mode)
        if action=='group-control':return group_monitor.control(payload,mode)
        if action=='group-exit-admin-only':return group_lifecycle.control(payload,mode)
        if action=='group-discovery-control':return group_discovery.control(payload,mode)
        if action=='group-question':return group_discovery.question(payload,mode)
        if action=='group-answer':return group_discovery.answer(payload,mode)
        raise ValueError('群操作不存在')
