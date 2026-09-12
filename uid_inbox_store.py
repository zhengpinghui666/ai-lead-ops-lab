"""Selected-lead HTTP inbox history. Never grants contact or advances send jobs."""
import json
import re
import clubops as app
import uid_inbox
import uid_messaging
import uid_protocol as wire
import uid_session

SCHEMA = '''
CREATE TABLE IF NOT EXISTS uid_inbox_conversations (
 id INTEGER PRIMARY KEY, account_uid TEXT NOT NULL, peer_uid TEXT NOT NULL,
 conversation_id TEXT NOT NULL, conversation_short_id TEXT NOT NULL, inbox INTEGER NOT NULL,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, next_cursor TEXT, has_more INTEGER,
 UNIQUE(account_uid,conversation_id,inbox)
);
CREATE TABLE IF NOT EXISTS uid_inbox_messages (
 id INTEGER PRIMARY KEY, account_uid TEXT NOT NULL, conversation_id TEXT NOT NULL,
 server_message_id TEXT NOT NULL, peer_uid TEXT NOT NULL, sender_uid TEXT NOT NULL,
 direction TEXT NOT NULL, content TEXT NOT NULL, platform_index TEXT NOT NULL,
 created_at_raw TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 read_id INTEGER NOT NULL,
 UNIQUE(account_uid,conversation_id,server_message_id)
);
CREATE INDEX IF NOT EXISTS idx_uid_inbox_peer ON uid_inbox_messages(account_uid,peer_uid,id);
CREATE TABLE IF NOT EXISTS uid_inbox_reads (
 id INTEGER PRIMARY KEY, account_uid TEXT NOT NULL, peer_uid TEXT NOT NULL,
 lead_id INTEGER NOT NULL REFERENCES leads(id), operation TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, detail TEXT NOT NULL DEFAULT '{}'
);
'''


def _integer(value, minimum=1):
    if type(value) is not int or not minimum <= value <= 2**53-1:
        raise ValueError('收件记录参数无效')
    return value


def _target(c, lead_id):
    row=c.execute('''SELECT p.external_id,s.kind FROM leads l JOIN people p ON p.id=l.person_id
        JOIN sources s ON s.id=p.source_id WHERE l.id=?''',(_integer(lead_id),)).fetchone()
    if not row or row['kind'] not in ('browser','uid_test'):
        raise ValueError('仅支持已关联平台数字 UID 的采集线索')
    return wire.numeric_uid(row['external_id'])


def _account():
    settings,_=uid_messaging.config()
    try:return wire.numeric_uid(settings.get('sender_uid'))
    except ValueError:raise ValueError('请先在私信通道配置中核对当前账号') from None


def _session(account, provider):
    current=provider.current()
    if current['sender_uid']!=account or current.get('im_verified') is not True:
        raise ValueError('IM 会话身份尚未核对')


def history(lead_id, mode='live', before=0):
    if mode!='live':raise ValueError('演示区不读取平台私信')
    account=_account();before=_integer(before,0)
    with app.db(mode) as c:
        peer=_target(c,lead_id)
        rows=[dict(r) for r in c.execute('''SELECT * FROM uid_inbox_messages
          WHERE account_uid=? AND peer_uid=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 51''',(account,peer,before,before))]
        conversations=[dict(r) for r in c.execute('SELECT * FROM uid_inbox_conversations WHERE account_uid=? AND peer_uid=? ORDER BY id',(account,peer))]
        last=c.execute('SELECT * FROM uid_inbox_reads WHERE account_uid=? AND peer_uid=? ORDER BY id DESC LIMIT 1',(account,peer)).fetchone()
    ready=True
    try:_session(account,uid_session.Provider())
    except Exception:ready=False
    return dict(account_uid=account,peer_uid=peer,lead_id=lead_id,can_read=ready,
        session_detail='每次读取会重新核对登录身份' if ready else '本机 IM 会话需要重新准备，已保存记录仍可查看',
        messages=rows[:50],has_more=len(rows)>50,next_before=rows[49]['id'] if len(rows)>50 else None,
        conversations=conversations,last_read={**dict(last),'detail':json.loads(last['detail'])} if last else None)


def _conversation(row):
    return {key:row[key] for key in ('conversation_id','conversation_short_id','peer_uid','inbox')}


def _record_conversations(c,account,peer,result,stamp):
    # Scan has already checked participants and response identity. Save only this selected peer.
    rows=result.get('targets',{}).get(peer,{}).get('conversations',[])
    for row in rows:
        if row['peer_uid']!=peer:raise ValueError('会话与选定线索不一致')
        prior=c.execute('SELECT * FROM uid_inbox_conversations WHERE account_uid=? AND conversation_id=? AND inbox=?',(account,row['conversation_id'],row['inbox'])).fetchone()
        if prior and any(prior[key]!=row[key] for key in ('peer_uid','conversation_short_id')):
            raise ValueError('已有会话标识发生变化，保留此前记录')
        c.execute('''INSERT INTO uid_inbox_conversations(account_uid,peer_uid,conversation_id,conversation_short_id,inbox,first_seen_at,last_seen_at)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_uid,conversation_id,inbox) DO UPDATE SET last_seen_at=excluded.last_seen_at''',
          (account,peer,row['conversation_id'],row['conversation_short_id'],row['inbox'],stamp,stamp))


def _record_messages(c,account,peer,conversation,result,stamp,read_id,cursor):
    if result['status']!='messages_observed':return 0
    next_cursor=result.get('next_cursor')
    if result.get('has_more') and (not next_cursor or next_cursor in ('0',cursor)):
        raise ValueError('分页没有前进，保留此前记录')
    added=0
    for row in result['messages']:
        author=wire.numeric_uid(row['sender_uid'])
        if author not in (account,peer) or row['direction']!=('outbound' if author==account else 'inbound'):
            raise ValueError('消息作者与会话不一致')
        content=row['content']
        if not isinstance(content,str) or not content.strip() or len(content)>5000:raise ValueError('消息正文无效')
        for key in ('server_message_id','index','created_at_raw'):
            if not isinstance(row[key],str) or not re.fullmatch(r'[0-9]{1,19}',row[key]):raise ValueError('消息标识无效')
        if int(row['server_message_id'])<1:raise ValueError('消息标识无效')
        key=(account,conversation['conversation_id'],row['server_message_id'])
        prior=c.execute('SELECT * FROM uid_inbox_messages WHERE account_uid=? AND conversation_id=? AND server_message_id=?',key).fetchone()
        if prior:
            if any(prior[k]!=v for k,v in dict(peer_uid=peer,sender_uid=author,direction=row['direction'],content=content,platform_index=row['index'],created_at_raw=row['created_at_raw']).items()):
                raise ValueError('同一消息内容发生变化，保留此前记录')
            c.execute('UPDATE uid_inbox_messages SET last_seen_at=? WHERE id=?',(stamp,prior['id']))
        else:
            c.execute('''INSERT INTO uid_inbox_messages(account_uid,conversation_id,server_message_id,peer_uid,sender_uid,direction,content,platform_index,created_at_raw,first_seen_at,last_seen_at,read_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(*key,peer,author,row['direction'],content,row['index'],row['created_at_raw'],stamp,stamp,read_id))
            added+=1
    c.execute('UPDATE uid_inbox_conversations SET next_cursor=?,has_more=?,last_seen_at=? WHERE id=?',
              (next_cursor,result.get('has_more'),stamp,conversation['id']))
    return added


def read(body, mode='live', *, provider=None, exchange=None):
    if mode!='live' or set(body) not in ({'lead_id','account_uid','operation'}, {'lead_id','account_uid','operation','conversation_id','older'}):
        raise ValueError('收件参数无效')
    operation=body['operation']
    if operation not in ('scan','messages') or (operation=='scan')!=('older' not in body):raise ValueError('收件操作无效')
    if not uid_messaging.GUARD.acquire(blocking=False):raise ValueError('已有私信读取或发送正在处理，请等待完成')
    read_id=None
    try:
        account=_account()
        if account!=body['account_uid']:raise ValueError('当前账号已变化，请重新打开收件记录')
        with app.LOCKS[mode],app.db(mode) as c:
            peer=_target(c,body['lead_id'])
            if peer==account:raise ValueError('不能将当前账号作为收件对象')
            conversation=None;cursor='0'
            if operation=='messages':
                if type(body['older']) is not bool:raise ValueError('分页参数无效')
                row=c.execute('SELECT * FROM uid_inbox_conversations WHERE id=? AND account_uid=? AND peer_uid=?',(_integer(body['conversation_id']),account,peer)).fetchone()
                if not row:raise ValueError('请先核对该线索的已有会话')
                conversation=dict(row)
                if body['older']:
                    if row['inbox']!=0 or not row['has_more'] or not row['next_cursor']:raise ValueError('没有可继续读取的分页')
                    cursor=row['next_cursor']
            read_id=c.execute('INSERT INTO uid_inbox_reads(account_uid,peer_uid,lead_id,operation,status,started_at) VALUES(?,?,?,?,?,?)',
                             (account,peer,body['lead_id'],operation,'reading',app.now())).lastrowid
        provider=provider or uid_session.Provider()
        try:_session(account,provider)
        except Exception:
            result=dict(status='session_unavailable',error='local_session_unavailable')
        else:
            if operation=='scan':result=uid_inbox.scan(account,[peer],provider=provider,exchange=exchange,max_pages=3,page_size=20)
            else:result=uid_inbox.messages(account,_conversation(conversation),provider=provider,exchange=exchange,limit=20,cursor=int(cursor))
        # Never persist credential envelopes. Only the existing reader's bounded diagnostics.
        detail={k:result[k] for k in ('status','error','scopes','evidence','returned_count','skipped_count','output_truncated','has_more','next_cursor') if k in result}
        added=0;stamp=app.now()
        with app.LOCKS[mode],app.db(mode) as c:
            if _account()!=account or _target(c,body['lead_id'])!=peer:raise ValueError('读取期间账号或线索改变，未保存消息')
            if operation=='scan':_record_conversations(c,account,peer,result,stamp)
            elif conversation:added=_record_messages(c,account,peer,conversation,result,stamp,read_id,cursor)
            detail['new_messages']=added
            c.execute('UPDATE uid_inbox_reads SET status=?,finished_at=?,detail=? WHERE id=?',
                      (result['status'],stamp,json.dumps(detail,ensure_ascii=False),read_id))
        return dict(read_id=read_id,status=result['status'],new_messages=added)
    except Exception:
        if read_id:
            with app.LOCKS[mode],app.db(mode) as c:
                c.execute("UPDATE uid_inbox_reads SET status='read_failed',finished_at=?,detail=? WHERE id=?",(app.now(),json.dumps({'error':'read_or_persistence_failed'}),read_id))
        raise
    finally:uid_messaging.GUARD.release()


def recover(mode='live'):
    with app.LOCKS[mode],app.db(mode) as c:
        c.execute("UPDATE uid_inbox_reads SET status='interrupted',finished_at=? WHERE status='reading'",(app.now(),))
