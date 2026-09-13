"""Durable, own-account group observation. Group traffic is exclusively reads."""
import json
import demand_freshness
import re
import threading
from datetime import datetime, timezone, timedelta

import clubops as app
import group_inbox
import uid_inbox_store
import uid_messaging
from game_scope import in_pc_scope, record_exclusion

GUARD = threading.RLock()
STOP = threading.Event()
THREAD = None
ACTIVE = False
POLICY_KEY = 'group_monitor_policy'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS monitored_groups (
 id INTEGER PRIMARY KEY, account_uid TEXT NOT NULL, conversation_id TEXT NOT NULL,
 conversation_short_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, notice TEXT NOT NULL,
 member INTEGER NOT NULL, participants INTEGER NOT NULL, matched INTEGER NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, checked_at TEXT NOT NULL, next_run_at TEXT,
 status TEXT NOT NULL DEFAULT 'available', detail TEXT NOT NULL DEFAULT '',
 watermark TEXT NOT NULL DEFAULT '0', cursor TEXT NOT NULL DEFAULT '0', cycle_head TEXT NOT NULL DEFAULT '0',
 last_read_at TEXT, failures INTEGER NOT NULL DEFAULT 0,
 UNIQUE(account_uid,conversation_id)
);
CREATE TABLE IF NOT EXISTS group_messages (
 id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES monitored_groups(id),
 message_id TEXT NOT NULL, uid TEXT NOT NULL, person_id INTEGER REFERENCES people(id),
 raw_text TEXT NOT NULL, group_title TEXT NOT NULL, message_index TEXT NOT NULL,
 published_at TEXT, observed_at TEXT NOT NULL, filter_reason TEXT NOT NULL,
 relevance TEXT NOT NULL, category TEXT NOT NULL, game TEXT NOT NULL, reason TEXT NOT NULL,
 analysis_method TEXT NOT NULL DEFAULT 'rules', facts TEXT NOT NULL,
 UNIQUE(group_id,message_id)
);
CREATE INDEX IF NOT EXISTS idx_group_messages_person ON group_messages(person_id,id);
CREATE TABLE IF NOT EXISTS group_reads (
 id INTEGER PRIMARY KEY, group_id INTEGER REFERENCES monitored_groups(id), account_uid TEXT NOT NULL,
 operation TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
'''
SELECT = '''SELECT m.*,g.name AS current_group_name,g.account_uid,p.nickname,l.id AS lead_id
 FROM group_messages m JOIN monitored_groups g ON g.id=m.group_id
 LEFT JOIN people p ON p.id=m.person_id LEFT JOIN leads l ON l.person_id=m.person_id'''


def stamp_after(seconds):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def fresh(value, seconds=600):
    try:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(value)).total_seconds()
        return 0 <= age <= seconds
    except (ValueError,TypeError):return False


def match(group):
    return in_pc_scope(' '.join(group.get(k,'') for k in ('name','description','notice')))


def policy(c):
    row=c.execute('SELECT value FROM settings WHERE key=?',(POLICY_KEY,)).fetchone()
    return json.loads(row[0]) if row else {}


def discover(*,reader=None):
    account=uid_inbox_store._account();reader=reader or group_inbox.catalog
    rows=[];proof=[];cursor=0
    for _ in range(5):
        result=reader(account,cursor=cursor)
        rows.extend(result['groups']);proof.extend(result['evidence'])
        if not result['has_more']:break
        cursor=int(result['next_cursor'])
    complete=not result['has_more']
    if len({r['conversation_id'] for r in rows})!=len(rows):raise ValueError('群目录分页重复；未覆盖已有目录')
    with app.LOCKS['live'],app.db() as c:
        for row in rows:
            matched=match(row)
            c.execute('''INSERT INTO monitored_groups(account_uid,conversation_id,conversation_short_id,
              name,description,notice,member,participants,matched,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(account_uid,conversation_id) DO UPDATE SET
              conversation_short_id=excluded.conversation_short_id,name=excluded.name,description=excluded.description,
              notice=excluded.notice,member=excluded.member,participants=excluded.participants,
              matched=excluded.matched,checked_at=excluded.checked_at,
              enabled=CASE WHEN excluded.member AND excluded.matched THEN monitored_groups.enabled ELSE 0 END''',
              (account,row['conversation_id'],row['conversation_short_id'],row['name'],row['description'],row['notice'],
               int(row['member']),row['participants'],int(matched),app.now()))
        if complete:
            seen={r['conversation_id'] for r in rows}
            for old in c.execute('SELECT id,conversation_id FROM monitored_groups WHERE account_uid=?',(account,)).fetchall():
                if old['conversation_id'] not in seen:
                    c.execute("UPDATE monitored_groups SET member=0,enabled=0,status='unavailable',detail='当前账号群目录已无此群' WHERE id=?",(old['id'],))
        c.execute('INSERT INTO group_reads(account_uid,operation,status,detail,created_at) VALUES(?,?,?,?,?)',
                  (account,'catalog','completed',json.dumps(dict(count=len(rows),complete=complete,evidence=proof)),app.now()))
    return dict(discovered=len(rows),matched=sum(match(r) and r['member'] for r in rows),complete=complete)


def refresh(mode='live'):
    if mode!='live':raise ValueError('请在正式工作区读取群目录')
    with GUARD:
        if not uid_messaging.GUARD.acquire(blocking=False):raise ValueError('IM 通道正在处理，请稍后刷新群目录')
        try:return discover()
        finally:uid_messaging.GUARD.release()


def control(body,mode='live'):
    if mode!='live' or type(body.get('enabled')) is not bool:raise ValueError('群监控开关无效')
    rid=uid_inbox_store._integer(body.get('id'),1)
    account=uid_inbox_store._account()
    with GUARD,app.LOCKS[mode],app.db(mode) as c:
        row=c.execute('SELECT * FROM monitored_groups WHERE id=? AND account_uid=?',(rid,account)).fetchone()
        if not row:raise ValueError('当前账号群记录不存在')
        if body['enabled'] and (not row['member'] or not row['matched'] or not match(dict(row)) or not fresh(row['checked_at'])):
            raise ValueError('请先刷新群目录，仅能监控已加入且符合范围的群')
        if body['enabled'] and c.execute('SELECT COUNT(*) FROM monitored_groups WHERE enabled=1 AND id<>? AND account_uid=?',(rid,account)).fetchone()[0]>=5:
            raise ValueError('当前最多同时监控 5 个群')
        c.execute('UPDATE monitored_groups SET enabled=?,status=?,detail=?,next_run_at=? WHERE id=?',
                  (int(body['enabled']),'waiting' if body['enabled'] else 'paused',
                   '等待读取；群内不发言' if body['enabled'] else '用户暂停群监控',app.now(),rid))
        app.event(c,'group_monitor',f'群监控 #{rid} '+('开启' if body['enabled'] else '暂停'))
    return state(mode)


def authorize_outreach(instruction,account):
    if account!=uid_inbox_store._account() or not isinstance(instruction,str) or not instruction.strip():
        raise ValueError('需要当前账号明确的群消息私信授权')
    with GUARD,app.LOCKS['live'],app.db() as c:
        c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                  (POLICY_KEY,json.dumps(dict(account_uid=account,outreach_enabled=True,instruction=instruction,granted_at=app.now()),ensure_ascii=False)))


def routing(c,rid):
    row=c.execute('''SELECT m.*,g.enabled,g.member,g.matched,g.checked_at,g.account_uid
      FROM group_messages m JOIN monitored_groups g ON g.id=m.group_id WHERE m.id=?''',(rid,)).fetchone()
    if not row:raise ValueError('群原文不存在')
    relevance=json.loads(row['relevance'])
    excluded=record_exclusion(c,'group',rid)
    allowed=bool(not row['filter_reason'] and relevance['passed'] and row['enabled'] and row['member']
                 and row['matched'] and fresh(row['checked_at']) and demand_freshness.assess(row['published_at'])['eligible']
                 and row['account_uid']==uid_inbox_store._account())
    reason='已加入的对口群消息通过初筛，进入模型。' if allowed else row['filter_reason'] or '群监控已关闭、成员身份待更新或消息不在一天内。'
    if excluded:allowed=False;reason=excluded
    return dict(version='group-pc-v1',model_allowed=allowed,route='model' if allowed else 'keywords',reason=reason,
                asset_kind='group',asset_key=str(row['group_id']),keyword_match=relevance,learned_keywords=[])


def attach(c,uid,observed):
    source=c.execute("SELECT id FROM sources WHERE kind='browser' ORDER BY id LIMIT 1").fetchone()
    sid=source[0] if source else c.execute("INSERT INTO sources(name,kind,status,notes) VALUES('抖音 · 数字 UID','browser','unverified','群消息与评论、弹幕分别保留原文')").lastrowid
    c.execute('INSERT OR IGNORE INTO people(source_id,external_id,nickname) VALUES(?,?,?)',(sid,uid,'未提供昵称'))
    pid=c.execute('SELECT id FROM people WHERE source_id=? AND external_id=?',(sid,uid)).fetchone()[0]
    c.execute('INSERT OR IGNORE INTO leads(person_id,updated_at) VALUES(?,?)',(pid,observed))
    return pid


def ingest(c,group,result):
    import asset_keywords
    import analysis_store
    inserted=[]
    for message in result['messages']:
        if int(message['index'])<=int(group['watermark']):continue
        published=uid_inbox_store.message_timestamp(message['created_at_raw'])
        text=message['raw_text'];title=group['name'];relevance=asset_keywords.message_relevance(c,text,title)
        filtered=('自己发送的消息' if message['uid']==group['account_uid'] else demand_freshness.assess(published)['detail'] if not demand_freshness.assess(published)['eligible']
                  else '未通过陪玩需求初筛' if not relevance['passed'] else '')
        classification=app.classify(text,title)
        pid=attach(c,message['uid'],app.now()) if not filtered else None
        cur=c.execute('''INSERT OR IGNORE INTO group_messages(group_id,message_id,uid,person_id,raw_text,group_title,
          message_index,published_at,observed_at,filter_reason,relevance,category,game,reason,facts)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (group['id'],message['message_id'],message['uid'],pid,text,title,message['index'],published,app.now(),filtered,
           json.dumps(relevance,ensure_ascii=False),classification['category'],classification['game'],classification['reason'],
           json.dumps(classification['facts'],ensure_ascii=False)))
        if cur.rowcount:
            analysis_store.capture_rule(c,'group',cur.lastrowid)
            if not filtered:inserted.append(cur.lastrowid)
    return inserted


def project(c,raw,*,engine=None):
    import analysis_store
    row=dict(raw);row['facts']=json.loads(row['facts']);row['rule_facts']=row['facts'];row['rule_game']=row['game']
    row.update(evidence_type='group',source_title=row['group_title'],source_url='',source_name='抖音群聊',
               external_id=row['message_id'],discovered_at=row['observed_at'],parent_context={'status':'none'},
               confidence=None,manual_fields={},review_history=[])
    analysis_store.project(c,row,model_engine=engine)
    if row['analysis_method']=='rules' and row['category']=='buyer':
        row.update(category='uncertain',reason='初筛发现可能的需求，等待模型确认。')
    return row


def eligible(c,raw,engine,account):
    grant=policy(c)
    if not grant.get('outreach_enabled') or grant.get('account_uid')!=account or raw['account_uid']!=account:return False
    if not routing(c,raw['id'])['model_allowed']:return False
    row=project(c,raw,engine=engine)
    return row['analysis_method']=='model' and row['category']=='buyer' and row['game']==app.TARGET_GAME


def tick(*,reader=None,catalog_reader=None):
    import uid_session_renewal
    if uid_session_renewal.pending():
        return
    global ACTIVE
    if STOP.is_set() or not GUARD.acquire(blocking=False):return
    acquired=False;group=None
    try:
        with app.db() as c:
            group=c.execute('SELECT * FROM monitored_groups WHERE enabled=1 AND account_uid=? AND next_run_at<=? ORDER BY next_run_at,id LIMIT 1',(uid_inbox_store._account(),app.now())).fetchone()
        if not group:return
        acquired=uid_messaging.GUARD.acquire(blocking=False)
        if not acquired:return
        ACTIVE=True
        discover(reader=catalog_reader)
        with app.db() as c:group=dict(c.execute('SELECT * FROM monitored_groups WHERE id=?',(group['id'],)).fetchone())
        if not group['enabled'] or group['account_uid']!=uid_inbox_store._account():return
        reader=reader or group_inbox.messages
        for _ in range(3):
            if STOP.is_set():break
            result=reader(group['account_uid'],dict(group,inbox=0),cursor=int(group['cursor']))
            cursor=int(result['next_cursor']);head=max(int(group['cycle_head']),int(result['maximum_index'] or 0))
            # Stop at the saved index, exhausted history, or an entirely expired text page.
            old_page=bool(result['messages']) and all(demand_freshness.assess(uid_inbox_store.message_timestamp(m['created_at_raw']))['status']=='expired' for m in result['messages'])
            done=not result['has_more'] or result['minimum_index'] is not None and int(result['minimum_index'])<=int(group['watermark']) or old_page
            with app.LOCKS['live'],app.db() as c:
                ingest(c,group,result)
                c.execute('''UPDATE monitored_groups SET watermark=?,cursor=?,cycle_head=?,last_read_at=?,next_run_at=?,
                  status=?,detail=?,failures=0 WHERE id=?''',
                  (str(max(head,int(group['watermark']))) if done else group['watermark'],'0' if done else str(cursor),'0' if done else str(head),
                   app.now(),stamp_after(60),'running' if done else 'catching_up','读取完成；群内不发言',group['id']))
                c.execute('INSERT INTO group_reads(group_id,account_uid,operation,status,detail,created_at) VALUES(?,?,?,?,?,?)',
                          (group['id'],group['account_uid'],'messages','completed',json.dumps({k:v for k,v in result.items() if k!='messages'}),app.now()))
                group=dict(c.execute('SELECT * FROM monitored_groups WHERE id=?',(group['id'],)).fetchone())
            if done:break
    except Exception as exc:
        if group is not None:
            # Never persist raw transport errors: they may contain session material.
            detail='群读取未完成；已保存进度，稍后重试。'+(' '+str(exc) if isinstance(exc,group_inbox.inbox.ReadError) else '')
            with app.LOCKS['live'],app.db() as c:
                c.execute("UPDATE monitored_groups SET status='retrying',detail=?,failures=failures+1,next_run_at=? WHERE id=?",
                          (detail,stamp_after(min(600,60*2**min(3,group['failures']))),group['id']))
                c.execute('INSERT INTO group_reads(group_id,account_uid,operation,status,detail,created_at) VALUES(?,?,?,?,?,?)',
                          (group['id'],group['account_uid'],'messages','failed',json.dumps(dict(detail=detail)),app.now()))
    finally:
        ACTIVE=False
        if acquired:uid_messaging.GUARD.release()
        GUARD.release()
    replenish()


def replenish():
    import semantic_queue
    reconsider_legacy_time_filters()
    with app.db() as c:
        ids=[r[0] for r in c.execute("SELECT id FROM group_messages WHERE filter_reason='' AND published_at>=? ORDER BY id DESC LIMIT 200",(stamp_after(-demand_freshness.MAX_AGE_SECONDS),))]
    if ids:semantic_queue.enqueue('group',ids)


def reconsider_legacy_time_filters():
    """Apply the confirmed one-day rule without erasing the old filter evidence."""
    import asset_keywords
    account = uid_inbox_store._account()
    changed = []
    with GUARD, app.LOCKS['live'], app.db() as c:
        rows = c.execute('''SELECT m.*,g.account_uid,g.checked_at FROM group_messages m
            JOIN monitored_groups g ON g.id=m.group_id
            WHERE m.filter_reason='时间缺失或消息超过 1 小时'
            AND g.account_uid=? AND g.enabled=1 AND g.member=1 AND g.matched=1''', (account,)).fetchall()
        for row in rows:
            timing = demand_freshness.assess(row['published_at'])
            if not fresh(row['checked_at']) or not timing['eligible']:
                continue
            relevance = asset_keywords.message_relevance(c, row['raw_text'], row['group_title'])
            excluded = record_exclusion(c, 'group', row['id'])
            filtered = ('自己发送的消息' if row['uid'] == account else excluded or
                        ('未通过陪玩需求初筛' if not relevance['passed'] else ''))
            person_id = attach(c, row['uid'], row['observed_at']) if not filtered else row['person_id']
            detail = dict(record_id=row['id'], policy='demand-window-24h-v1',
                          previous_filter_reason=row['filter_reason'], previous_relevance=json.loads(row['relevance']),
                          filter_reason=filtered, relevance=relevance, timing=timing,
                          raw_text_preserved=True, classification_preserved=True)
            c.execute('INSERT INTO group_reads(group_id,account_uid,operation,status,detail,created_at) VALUES(?,?,?,?,?,?)',
                      (row['group_id'], account, 'rescreen', 'completed', json.dumps(detail, ensure_ascii=False), app.now()))
            c.execute('UPDATE group_messages SET filter_reason=?,relevance=?,person_id=? WHERE id=?',
                      (filtered, json.dumps(relevance, ensure_ascii=False), person_id, row['id']))
            changed.append(row['id'])
    return changed


def state(mode='live',before=0):
    import semantic
    import group_discovery
    if mode!='live':return dict(groups=[],messages=[],enabled=0)
    before=uid_inbox_store._integer(before,0)
    try:account=uid_inbox_store._account()
    except ValueError:return dict(groups=[],messages=[],enabled=0,issue='请先在私信通道配置中核对账号')
    engine=semantic.state()['engine']
    with app.db(mode) as c:
        groups=[dict(r) for r in c.execute('''SELECT g.*,(SELECT COUNT(*) FROM group_messages m WHERE m.group_id=g.id) AS message_count,
          (SELECT COUNT(*) FROM group_messages m WHERE m.group_id=g.id AND m.filter_reason='') AS screened_count
          FROM monitored_groups g WHERE account_uid=? AND (matched=1 OR enabled=1) ORDER BY enabled DESC,id''',(account,))]
        anchor=''
        if before:
            previous=c.execute(SELECT+' WHERE g.account_uid=? AND m.id=?',(account,before)).fetchone()
            if not previous:raise ValueError('群消息分页记录不存在')
            anchor=previous['published_at'] or ''
        rows=c.execute(SELECT+''' WHERE g.account_uid=? AND (?=0 OR COALESCE(m.published_at,'')<?
          OR COALESCE(m.published_at,'')=? AND m.id<?)
          ORDER BY COALESCE(m.published_at,'') DESC,m.id DESC LIMIT 51''',(account,before,anchor,anchor,before)).fetchall()
        messages=[project(c,r,engine=engine) for r in rows[:50]]
        for row in messages:
            attempt=c.execute('SELECT job_id,status,detail FROM uid_message_attempts WHERE sender_uid=? AND recipient_uid=? ORDER BY job_id DESC LIMIT 1',(account,row['uid'])).fetchone()
            row['outreach']=dict(attempt) if attempt else None
    return dict(account_uid=account,groups=groups,messages=messages,enabled=sum(g['enabled'] for g in groups),
                has_more=len(rows)>50,next_before=messages[-1]['id'] if len(rows)>50 else None,
                group_speaking=False,discovery_scope='public_and_joined',public_join_available=True,
                discovery=group_discovery.state())


def start_service():
    global THREAD
    if THREAD and THREAD.is_alive():return
    STOP.clear()
    def loop():
        while not STOP.wait(10):
            try:tick()
            except Exception:pass  # Read failures are durable; isolate this loop from comments/live.
            try:
                import group_discovery
                group_discovery.tick()
            except Exception:pass
    THREAD=threading.Thread(target=loop,name='group-monitor',daemon=True);THREAD.start()


def shutdown():
    STOP.set()
    if THREAD:THREAD.join()
