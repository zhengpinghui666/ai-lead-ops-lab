"""Durable, serial inbox reads for explicitly selected verified conversations.

No sending, conversation discovery, reply inference, read markers or model calls.
"""
import json
import threading
from datetime import datetime,timedelta,timezone
import clubops as app
import uid_inbox_store as inbox
import uid_messaging
import uid_session

SCHEMA='''
CREATE TABLE IF NOT EXISTS uid_inbox_sync (
 id INTEGER PRIMARY KEY,lead_id INTEGER NOT NULL REFERENCES leads(id),account_uid TEXT NOT NULL,
 peer_uid TEXT NOT NULL,conversation_id INTEGER NOT NULL REFERENCES uid_inbox_conversations(id),
 interval_seconds INTEGER NOT NULL DEFAULT 60,enabled INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'paused',detail TEXT NOT NULL DEFAULT '',next_run_at TEXT,
 last_read_id INTEGER REFERENCES uid_inbox_reads(id),failures INTEGER NOT NULL DEFAULT 0,
 read_count INTEGER NOT NULL DEFAULT 0,new_messages INTEGER NOT NULL DEFAULT 0,
 watermark TEXT NOT NULL DEFAULT '0',resume_cursor TEXT,cycle_head TEXT NOT NULL DEFAULT '0',
 seen_cursors TEXT NOT NULL DEFAULT '[]',updated_at TEXT NOT NULL,
 UNIQUE(account_uid,conversation_id)
);
'''
GUARD=threading.RLock()
STOP=threading.Event()
THREAD=None
ACTIVE=None

def _due(seconds):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(timespec='seconds')

def _binding(c,row):
    if inbox._account()!=row['account_uid'] or inbox._target(c,row['lead_id'])!=row['peer_uid']:
        raise ValueError('账号或线索已变化，请重新核对会话')
    conversation=c.execute('SELECT * FROM uid_inbox_conversations WHERE id=? AND account_uid=? AND peer_uid=?',
        (row['conversation_id'],row['account_uid'],row['peer_uid'])).fetchone()
    if not conversation or conversation['inbox']!=0:raise ValueError('后台同步目前仅支持已核对的普通会话')
    return conversation

def state(mode='live'):
    if mode!='live':return dict(targets=[],active_id=None,enabled_count=0)
    with GUARD,app.db() as c:
        rows=[dict(r) for r in c.execute('''SELECT s.*,p.nickname FROM uid_inbox_sync s
            JOIN leads l ON l.id=s.lead_id JOIN people p ON p.id=l.person_id ORDER BY s.id''')]
        return dict(targets=rows,active_id=ACTIVE,enabled_count=sum(bool(r['enabled']) for r in rows))

def save(body,mode='live'):
    if mode!='live' or set(body)!={'lead_id','account_uid','conversation_id','interval_seconds'}:raise ValueError('收件同步设置无效')
    for key in ('lead_id','conversation_id'):inbox._integer(body[key])
    if type(body['interval_seconds']) is not int or not 30<=body['interval_seconds']<=3600:raise ValueError('同步间隔需要为 30–3600 秒')
    with GUARD,app.LOCKS[mode],app.db() as c:
        peer=inbox._target(c,body['lead_id']);conversation=_binding(c,{**body,'peer_uid':peer})
        if not c.execute("SELECT 1 FROM uid_inbox_reads WHERE lead_id=? AND account_uid=? AND peer_uid=? AND operation='messages' AND status='messages_observed'",(body['lead_id'],body['account_uid'],peer)).fetchone():
            raise ValueError('请先成功读取一次该对象的消息')
        old=c.execute('SELECT * FROM uid_inbox_sync WHERE account_uid=? AND conversation_id=?',(body['account_uid'],body['conversation_id'])).fetchone()
        if old:
            if old['enabled'] or ACTIVE==old['id']:raise ValueError('请先暂停收件同步并等待读取结束')
            if old['lead_id']!=body['lead_id']:raise ValueError('该会话已关联另一线索，请先核对原关联')
            c.execute('UPDATE uid_inbox_sync SET interval_seconds=?,updated_at=? WHERE id=?',(body['interval_seconds'],app.now(),old['id']))
            return dict(id=old['id'],enabled=False)
        values=[int(r[0]) for r in c.execute('SELECT platform_index FROM uid_inbox_messages WHERE account_uid=? AND conversation_id=?',(body['account_uid'],conversation['conversation_id']))]
        rid=c.execute('''INSERT INTO uid_inbox_sync(lead_id,account_uid,peer_uid,conversation_id,interval_seconds,watermark,updated_at)
            VALUES(?,?,?,?,?,?,?)''',(body['lead_id'],body['account_uid'],peer,body['conversation_id'],body['interval_seconds'],str(max(values,default=0)),app.now())).lastrowid
        return dict(id=rid,enabled=False)

def control(body,enable,mode='live'):
    if mode!='live' or set(body)!={'id'} or type(enable) is not bool:raise ValueError('收件同步操作无效')
    rid=inbox._integer(body['id'])
    with GUARD,app.LOCKS[mode],app.db() as c:
        row=c.execute('SELECT * FROM uid_inbox_sync WHERE id=?',(rid,)).fetchone()
        if not row:raise ValueError('未找到收件同步设置')
        if enable:
            if STOP.is_set():raise ValueError('服务正在关闭，请稍后再开启')
            if ACTIVE==rid:raise ValueError('上一轮读取尚未结束')
            _binding(c,row);inbox._session(row['account_uid'],uid_session.Provider())
            if row['enabled']:return dict(id=rid,enabled=True)
        c.execute('UPDATE uid_inbox_sync SET enabled=?,status=?,detail=?,next_run_at=?,failures=0,updated_at=? WHERE id=?',
            (int(enable),'waiting' if enable else 'stopping' if ACTIVE==rid else 'paused',
             '等待后台同步已核对会话' if enable else '停止后续读取；正在返回的当前页会保存',app.now() if enable else None,app.now(),rid))
        return dict(id=rid,enabled=enable)

def tick():
    global ACTIVE
    with GUARD:
        if STOP.is_set() or ACTIVE is not None or uid_messaging.GUARD.locked():return None
        with app.LOCKS['live'],app.db() as c:
            row=c.execute('''SELECT * FROM uid_inbox_sync WHERE enabled=1 AND julianday(next_run_at)<=julianday(?)
                ORDER BY julianday(next_run_at),id LIMIT 1''',(app.now(),)).fetchone()
            if not row:return None
            row=dict(row);rid=row['id']
            c.execute("UPDATE uid_inbox_sync SET status='reading',detail='正在读取已核对会话',updated_at=? WHERE id=?",(app.now(),rid))
        ACTIVE=rid
    try:
        with app.db() as c:_binding(c,row)
        result=inbox.read(dict(lead_id=row['lead_id'],account_uid=row['account_uid'],operation='messages',conversation_id=row['conversation_id'],older=False),_cursor=row['resume_cursor'])
    except inbox.InboxBusy:
        result=dict(status='busy',error='inbox_busy')
    except Exception:
        result=dict(status='read_failed',error='binding_or_persistence_failed')
    try:
        with GUARD,app.LOCKS['live'],app.db() as c:
            current=c.execute('SELECT * FROM uid_inbox_sync WHERE id=?',(rid,)).fetchone()
            enabled=bool(current['enabled']) and not STOP.is_set();status=result['status'];failures=row['failures']
            updates=dict(updated_at=app.now(),enabled=int(enabled))
            if result.get('read_id'):
                updates.update(last_read_id=result['read_id'],read_count=row['read_count']+1,new_messages=row['new_messages']+result.get('new_inbound',0))
            if status=='messages_observed':
                failures=0;high=max(int(row['cycle_head']),int(row['watermark']),int(result.get('maximum_index') or 0))
                seen=json.loads(row['seen_cursors']);cursor=row['resume_cursor'] or '0';seen.append(cursor)
                next_cursor=result.get('next_cursor')
                reached=result.get('minimum_index') is not None and int(result['minimum_index'])<=int(row['watermark'])
                invalid=result.get('output_truncated') or result.get('has_more') is None or (result.get('has_more') and (not next_cursor or next_cursor in seen or len(seen)>=200))
                if invalid:
                    enabled=False;updates.update(enabled=0,status='attention',detail='分页范围无法确认，已保留结果和断点；请核对收件记录',next_run_at=None)
                elif reached or not result['has_more']:
                    updates.update(watermark=str(high),cycle_head='0',resume_cursor=None,seen_cursors='[]',status='waiting' if enabled else 'paused',
                        detail='本轮新消息已保存' if enabled else '已暂停；当前页已保存',next_run_at=_due(row['interval_seconds']) if enabled else None)
                else:
                    updates.update(cycle_head=str(high),resume_cursor=next_cursor,seen_cursors=json.dumps(seen),status='catching_up' if enabled else 'paused',
                        detail='正在按保存的游标追赶消息，完成后回到最新消息页',next_run_at=_due(5) if enabled else None)
            elif status=='busy':
                updates.update(status='waiting' if enabled else 'paused',detail='等待当前私信操作完成',next_run_at=_due(5) if enabled else None)
            else:
                failures+=1;transient=result.get('error')=='transport_failed' and failures<=3
                enabled=enabled and transient
                updates.update(enabled=int(enabled),status='retry_wait' if enabled else 'attention' if current['enabled'] else 'paused',
                    detail='网络读取未完成，保留断点并稍后重试' if enabled else '收件同步已暂停，请核对会话或读取诊断',
                    next_run_at=_due(60*2**(failures-1)) if enabled else None)
            updates['failures']=failures
            c.execute('UPDATE uid_inbox_sync SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',(*updates.values(),rid))
            return dict(id=rid,**result)
    finally:
        with GUARD:ACTIVE=None

def recover():
    with GUARD,app.LOCKS['live'],app.db() as c:
        c.execute("UPDATE uid_inbox_sync SET enabled=0,status='paused',next_run_at=NULL,detail='服务已重启或关闭；断点保留，需重新开启',updated_at=?",(app.now(),))

def start_service():
    global THREAD
    if THREAD and THREAD.is_alive():return
    STOP.clear()
    def loop():
        while not STOP.wait(2):
            try:tick()
            except Exception:
                recover();STOP.set()
    THREAD=threading.Thread(target=loop,name='uid-inbox-sync',daemon=True);THREAD.start()

def shutdown():
    STOP.set()
    if THREAD:THREAD.join()
    recover()
