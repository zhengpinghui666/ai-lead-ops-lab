"""Discover public groups from observed works; join only self, then verify membership."""
import json

import clubops as app
import group_monitor as monitor
import group_public
import uid_inbox_store
import uid_messaging

KEY='public_group_discovery'
QUERIES=['瓦搭子群','瓦开黑群','无畏契约开黑群','无畏契约搭子群','瓦群','国服瓦组队','打瓦找搭子','无畏契约交友群']
DAILY_JOIN_LIMIT=20  # Local scheduling budget, not a platform-approved quota.
OWNER_BATCH=3
SCHEMA='''
CREATE TABLE IF NOT EXISTS public_group_owners (
 account_uid TEXT NOT NULL,sec_uid TEXT NOT NULL,source_video TEXT NOT NULL,
 checked_at TEXT NOT NULL,next_check_at TEXT NOT NULL,status TEXT NOT NULL,
 PRIMARY KEY(account_uid,sec_uid)
);
CREATE TABLE IF NOT EXISTS public_group_candidates (
 account_uid TEXT NOT NULL,group_id TEXT NOT NULL,owner_sec_uid TEXT NOT NULL,
 name TEXT NOT NULL,description TEXT NOT NULL,participants INTEGER NOT NULL,list_status INTEGER NOT NULL,
 matched INTEGER NOT NULL,checked_at TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,
 PRIMARY KEY(account_uid,group_id)
);
CREATE TABLE IF NOT EXISTS public_group_attempts (
 account_uid TEXT NOT NULL,group_id TEXT NOT NULL,created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,status TEXT NOT NULL,proof TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(account_uid,group_id)
);
CREATE TABLE IF NOT EXISTS public_group_questions (
 account_uid TEXT NOT NULL,group_id TEXT NOT NULL,question TEXT NOT NULL,
 answer TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,
 PRIMARY KEY(account_uid,group_id)
);
CREATE TABLE IF NOT EXISTS public_group_answer_runs (
 id INTEGER PRIMARY KEY,account_uid TEXT NOT NULL,group_id TEXT NOT NULL,input_hash TEXT NOT NULL,
 question TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,finished_at TEXT,
 UNIQUE(account_uid,group_id,input_hash)
);
CREATE TABLE IF NOT EXISTS public_group_follows (
 account_uid TEXT NOT NULL,owner_uid TEXT NOT NULL,owner_sec_uid TEXT NOT NULL,
 created_at TEXT NOT NULL,status TEXT NOT NULL,proof TEXT NOT NULL DEFAULT '{}',
 PRIMARY KEY(account_uid,owner_uid)
);
'''
DETAILS=dict(candidate='等待核验入群条件',unmatched='群名称和介绍不符合范围',full='群已满',
 restricted='入群条件未通过',question='需要回答入群问题，等待模型作答',
 pending='已申请，等待群主审核',accepted='申请已被接受，等待成员身份确认',
 uncertain='申请结果未确认；保留记录，不重复提交',rejected='平台未通过申请，不重复提交',
 joined='已从当前账号群目录确认加入',observed='当前账号已在群内',
 unavailable='公开群已不可用',not_submitted='申请在本地准备阶段失败，尚未提交平台；等待修复',
 follow_wait='等待关注条件或关注时长满足，稍后复查',paid='需要付费条件，保留候选')


def config(c):
    import account_scope
    current=account_scope.current()
    main=c.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
    main=json.loads(main[0]) if main else dict(enabled=False,account_uid='',next_run_at='',detail='尚未开启公开群筛选')
    if not current or main.get('account_uid')==current['sender_uid']:return main
    row=c.execute('SELECT value FROM settings WHERE key=?',(KEY+':'+current['sender_uid'],)).fetchone()
    return json.loads(row[0]) if row else dict(main,account_uid=current['sender_uid'],next_run_at='',detail='等待为该账号核验公开群')


def save(c,value):
    main=c.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
    account=json.loads(main[0]).get('account_uid') if main else None
    key=KEY if not account or account==value.get('account_uid') else KEY+':'+value['account_uid']
    c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))


def search_terms(c):
    cfg=config(c)
    if not cfg.get('enabled'):return []
    try:
        if cfg.get('account_uid')!=uid_inbox_store._account():return []
        for word in QUERIES:c.execute('INSERT OR IGNORE INTO discovery_queries(keyword,next_check_at) VALUES(?,?)',(word,app.now()))
        return QUERIES
    except ValueError:return []


def control(body,mode='live'):
    if mode!='live' or set(body)!={'enabled'} or type(body.get('enabled')) is not bool:raise ValueError('公开群筛选开关无效')
    account=uid_inbox_store._account()
    with monitor.GUARD,app.LOCKS[mode],app.db(mode) as c:
        cfg=config(c)
        cfg.update(enabled=body['enabled'],account_uid=account,next_run_at=app.now(),
                   detail='等待筛选公开对口群；群内不发言' if body['enabled'] else '公开群筛选已暂停')
        save(c,cfg)
        for word in QUERIES:c.execute('INSERT OR IGNORE INTO discovery_queries(keyword,next_check_at) VALUES(?,?)',(word,app.now()))
        app.event(c,'group_discovery',cfg['detail'])
    return state()


def state():
    account=uid_inbox_store._account()
    with app.db() as c:
        cfg=config(c)
        counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) AS n FROM public_group_candidates WHERE account_uid=? GROUP BY status',(account,))}
        attempts=c.execute('SELECT COUNT(*) FROM public_group_attempts WHERE account_uid=? AND created_at>=?',(account,monitor.stamp_after(-86400))).fetchone()[0]
        owners=c.execute('SELECT COUNT(*) FROM public_group_owners WHERE account_uid=?',(account,)).fetchone()[0]
        pool=c.execute('''SELECT COUNT(DISTINCT w.author_sec_uid) FROM discovery_works w JOIN discovery_authors a
            ON a.sec_uid=w.author_sec_uid WHERE w.enabled=1 AND a.enabled=1 AND w.relevant=1''').fetchone()[0]
        candidates=[dict(r) for r in c.execute('''SELECT g.*,a.status AS application_status,a.updated_at AS application_at,
          COALESCE(q.question,'') AS question,COALESCE(q.answer,'') AS answer,CASE WHEN COALESCE(q.answer,'')!='' THEN 1 ELSE 0 END AS answered
          FROM public_group_candidates g LEFT JOIN public_group_attempts a
          ON a.account_uid=g.account_uid AND a.group_id=g.group_id
          LEFT JOIN public_group_questions q ON q.account_uid=g.account_uid AND q.group_id=g.group_id
          WHERE g.account_uid=? ORDER BY g.matched DESC,g.checked_at DESC,g.group_id LIMIT 100''',(account,))]
    return dict(enabled=bool(cfg.get('enabled') and cfg.get('account_uid')==account),detail=cfg.get('detail',''),
                next_run_at=cfg.get('next_run_at'),candidates=candidates,queries=QUERIES,counts=counts,
                coverage=dict(owners_checked=owners,owner_pool=pool,attempts_24h=attempts,join_budget=DAILY_JOIN_LIMIT))


def record(c,account,rows):
    for row in rows:
        matched=monitor.match(row)
        status='unmatched' if not matched else 'full' if row['list_status']==5 else 'unavailable' if row['list_status'] in (6,7) else 'candidate'
        attempt=c.execute('SELECT status FROM public_group_attempts WHERE account_uid=? AND group_id=?',(account,row['group_id'])).fetchone()
        if attempt:status=attempt[0]
        c.execute('''INSERT INTO public_group_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(account_uid,group_id) DO UPDATE SET owner_sec_uid=excluded.owner_sec_uid,name=excluded.name,
          description=excluded.description,participants=excluded.participants,list_status=excluded.list_status,
          matched=excluded.matched,checked_at=excluded.checked_at,status=excluded.status,detail=excluded.detail''',
          (account,row['group_id'],row['owner_sec_uid'],row['name'],row['description'],row['participants'],
           row['list_status'],int(matched),app.now(),status,DETAILS[status]))


def mark(c,account,gid,status):
    c.execute('UPDATE public_group_candidates SET status=?,detail=? WHERE account_uid=? AND group_id=?',
              (status,DETAILS[status],account,gid))


def reconcile(account,*,reader=None):
    monitor.discover(reader=reader)
    with app.LOCKS['live'],app.db() as c:
        for row in c.execute('''SELECT a.group_id,g.id,g.status,g.enabled,g.matched,g.member FROM public_group_attempts a
          JOIN monitored_groups g ON g.account_uid=a.account_uid AND g.conversation_id=a.group_id
          WHERE a.account_uid=? AND a.status!='joined' ''',(account,)).fetchall():
            import group_lifecycle
            if not row['member'] or group_lifecycle.excluded(c,account,row['group_id']):continue
            c.execute("UPDATE public_group_attempts SET status='joined',updated_at=? WHERE account_uid=? AND group_id=?",(app.now(),account,row['group_id']))
            mark(c,account,row['group_id'],'joined')
            # A manually paused group stays paused even if an application is later approved.
            import group_accounts
            if row['matched'] and row['status']=='available' and group_accounts.owns(c,row['group_id'],account):
                c.execute("UPDATE monitored_groups SET enabled=1,status='waiting',detail='已确认加入，等待读取；群内不发言',next_run_at=? WHERE id=?",(app.now(),row['id']))


def reconcile_application_status(account,*,client_factory=None):
    """A fresh, associated verification can positively prove pending approval.

    Never infer non-submission from absence, and never repeat a join operation.
    The original submission proof remains unchanged inside the saved receipt.
    """
    with app.db() as c:
        rows=c.execute('''SELECT g.* FROM public_group_candidates g JOIN public_group_attempts a USING(account_uid,group_id)
            WHERE a.account_uid=? AND a.status='uncertain'
            AND json_extract(a.proof,'$.submission_started')=1
            ORDER BY a.updated_at DESC LIMIT 1''',(account,)).fetchall()
    if not rows:return
    client=(client_factory or group_public.Client)(account)
    for row in rows:
        value=client.verify(dict(row))
        # Official PCIM verification maps 7601/7820 to HAS_APPLIED.
        if value.get('code') not in (7601,7820):continue
        with app.LOCKS['live'],app.db() as c:
            current=c.execute("SELECT proof FROM public_group_attempts WHERE account_uid=? AND group_id=? AND status='uncertain'",(account,row['group_id'])).fetchone()
            if not current:continue
            proof=json.loads(current['proof'])
            proof['status_verification']=dict(code=value['code'],checked_at=app.now(),group_id=row['group_id'],
                                             evidence=getattr(client,'evidence',[])[-1:])
            c.execute("UPDATE public_group_attempts SET status='pending',proof=?,updated_at=? WHERE account_uid=? AND group_id=?",
                      (json.dumps(proof,ensure_ascii=False),app.now(),account,row['group_id']))
            mark(c,account,row['group_id'],'pending')


def eligible(value):
    if not monitor.match(value):return 'unmatched'
    if value.get('code') in (7601,7820):return 'pending'
    if value.get('question') and not value.get('group_audit_answer','').strip():return 'question'
    if (value.get('code') not in (0,7602) or str(value.get('category'))!='2' or
            value.get('join_allowance') not in ('-1','1') or not isinstance(value.get('entry_limit'),list) or
            any(not isinstance(v,dict) or v.get('status')!=1 for v in value['entry_limit'])):return 'restricted'
    return ''


def remember_question(c,account,gid,question):
    if not isinstance(question,str) or not question.strip() or len(question)>1000:
        raise ValueError('入群问题缺失或超出范围')
    c.execute('''INSERT INTO public_group_questions VALUES(?,?,?,'',?)
        ON CONFLICT(account_uid,group_id) DO UPDATE SET question=excluded.question,
        answer=CASE WHEN public_group_questions.question=excluded.question THEN public_group_questions.answer ELSE '' END,
        updated_at=excluded.updated_at''',(account,gid,question,app.now()))
    return c.execute('SELECT answer FROM public_group_questions WHERE account_uid=? AND group_id=?',(account,gid)).fetchone()[0]


def follow_needs_recheck(old):
    if not old or old['status'] not in ('uncertain','not_submitted','deferred') or old['created_at']>monitor.stamp_after(-300):return False
    proof=json.loads(old['proof'])
    # A browser click with an unknown outcome is not replayed. Legacy HTTP
    # requests may switch transport only after reading an explicit unfollowed state.
    return proof.get('browser_version',0)<2 or proof.get('submission_started') is False


def prepare_follow(c,account,value,retry_proof=None):
    target=group_public.follow_requirement(value)
    if not target:return None
    old=c.execute('SELECT * FROM public_group_follows WHERE account_uid=? AND owner_uid=?',(account,target['uid'])).fetchone()
    if old:
        proof=json.loads(old['proof'])
        if not retry_proof or retry_proof.get('follow_status')!=0 or not follow_needs_recheck(old):return None
        previous={k:proof[k] for k in ('transport','submission_started','phase','http_status','reason','browser_attempted') if k in proof}
        history=dict(previous_status=old['status'],previous_proof=previous,retry_after_state_read=retry_proof,browser_attempted=True,browser_version=2)
        c.execute("UPDATE public_group_follows SET status='uncertain',created_at=?,proof=? WHERE account_uid=? AND owner_uid=?",(app.now(),json.dumps(history),account,target['uid']))
        return dict(target,history=history)
    count=c.execute('SELECT COUNT(*) FROM public_group_follows WHERE account_uid=? AND created_at>=?',(account,monitor.stamp_after(-86400))).fetchone()[0]
    if count>=DAILY_JOIN_LIMIT:return None
    history=dict(browser_attempted=True,browser_version=2)
    c.execute('INSERT INTO public_group_follows VALUES(?,?,?,?,?,?)',(account,target['uid'],target['sec_uid'],app.now(),'uncertain',json.dumps(history)))
    return dict(target,history=history)


def unmet_detail(value):
    names=[r.get('task_name','') for r in value.get('entry_limit',[]) if isinstance(r,dict) and r.get('status')!=1]
    names=[n for n in names if isinstance(n,str) and n.strip()]
    return '；'.join(names)[:500] or DETAILS['restricted']


def question(body,mode='live',*,client_factory=None):
    if mode!='live' or set(body)!={'group_id'}:raise ValueError('请选择有效的公开群')
    gid=group_public.wire.numeric_uid(body['group_id']);account=uid_inbox_store._account()
    with monitor.GUARD,uid_messaging.GUARD:
        with app.db() as c:
            row=c.execute('SELECT * FROM public_group_candidates WHERE account_uid=? AND group_id=? AND matched=1',(account,gid)).fetchone()
            if not row or c.execute('SELECT 1 FROM public_group_attempts WHERE account_uid=? AND group_id=?',(account,gid)).fetchone():
                raise ValueError('该群不存在、范围不符或已有申请记录')
        value=(client_factory or group_public.Client)(account).verify(dict(row))
        if not monitor.match(value):raise ValueError('该群当前不符合范围')
        with app.LOCKS[mode],app.db(mode) as c:
            saved=remember_question(c,account,gid,value['question'])
            mark(c,account,gid,'candidate' if saved else 'question')
    return state()


def answer(body,mode='live'):
    if mode!='live' or set(body)!={'group_id','question','answer'}:raise ValueError('入群回答参数无效')
    gid=group_public.wire.numeric_uid(body['group_id']);account=uid_inbox_store._account()
    text=body['answer']
    if not isinstance(text,str) or not text.strip() or len(text)>500:raise ValueError('请填写真实的入群回答')
    with monitor.GUARD,app.LOCKS[mode],app.db(mode) as c:
        row=c.execute('SELECT question FROM public_group_questions WHERE account_uid=? AND group_id=?',(account,gid)).fetchone()
        if not row or row['question']!=body['question']:raise ValueError('入群问题已变化，请重新读取')
        if c.execute('SELECT 1 FROM public_group_attempts WHERE account_uid=? AND group_id=?',(account,gid)).fetchone():
            raise ValueError('已有申请记录，不重复提交')
        c.execute('UPDATE public_group_questions SET answer=?,updated_at=? WHERE account_uid=? AND group_id=?',(text.strip(),app.now(),account,gid))
        mark(c,account,gid,'candidate')
        cfg=config(c);cfg.update(next_run_at=app.now());save(c,cfg)
    return state()


def run(account,*,client_factory=None,catalog_reader=None):
    reconcile(account,reader=catalog_reader)
    reconcile_application_status(account,client_factory=client_factory)
    with app.db() as c:
        sources=c.execute('''WITH source_rows AS (SELECT w.author_sec_uid,w.video_id,w.title,w.last_seen_at,
          o.checked_at,ROW_NUMBER() OVER(PARTITION BY w.author_sec_uid ORDER BY instr(w.title,'群')>0 DESC,w.last_seen_at DESC,w.video_id) AS rn
          FROM discovery_works w
          JOIN discovery_authors a ON a.sec_uid=w.author_sec_uid
          LEFT JOIN public_group_owners o ON o.account_uid=? AND o.sec_uid=w.author_sec_uid
          WHERE w.enabled=1 AND a.enabled=1 AND w.relevant=1
          AND (o.next_check_at IS NULL OR o.next_check_at<=?))
          SELECT * FROM source_rows WHERE rn=1
          ORDER BY COALESCE(checked_at,''),instr(title,'群')>0 DESC,last_seen_at DESC,author_sec_uid LIMIT ?''',(account,app.now(),OWNER_BATCH)).fetchall()
    import group_accounts
    with app.LOCKS['live'],app.db() as c:
        transfer_sources=group_accounts.sources_for(c,account)
    combined=[dict(author_sec_uid=sec,video_id='') for sec in transfer_sources]+[dict(r) for r in sources]
    seen=set();sources=[]
    for source in combined:
        if source['author_sec_uid'] not in seen:
            sources.append(source);seen.add(source['author_sec_uid'])
    sources=sources[:OWNER_BATCH]
    client=None
    for source in sources:
        with app.db() as c:cfg=config(c)
        if monitor.STOP.is_set() or not cfg.get('enabled') or cfg.get('account_uid')!=account:return
        client=client or (client_factory or group_public.Client)(account)
        try:
            rows=client.catalog(source['author_sec_uid'])
        except Exception:
            # A bad author cannot starve the whole author pool indefinitely.
            with app.LOCKS['live'],app.db() as c:
                c.execute('INSERT OR REPLACE INTO public_group_owners VALUES(?,?,?,?,?,?)',
                    (account,source['author_sec_uid'],source['video_id'],app.now(),monitor.stamp_after(1800),'retrying'))
            continue
        with app.LOCKS['live'],app.db() as c:
            record(c,account,rows)
            c.execute('INSERT OR REPLACE INTO public_group_owners VALUES(?,?,?,?,?,?)',
                      (account,source['author_sec_uid'],source['video_id'],app.now(),monitor.stamp_after(21600),'checked'))
    with app.db() as c:
        cfg=config(c)
        if monitor.STOP.is_set() or not cfg.get('enabled') or cfg.get('account_uid')!=account:return
        today=c.execute('SELECT COUNT(*) FROM public_group_attempts WHERE account_uid=? AND created_at>=?',(account,monitor.stamp_after(-86400))).fetchone()[0]
        if today>=DAILY_JOIN_LIMIT:return
        candidates=c.execute('''SELECT g.* FROM public_group_candidates g WHERE g.account_uid=? AND g.matched=1
          AND (g.status='candidate' OR (g.status='follow_wait' AND g.checked_at<=?))
          AND g.list_status IN (0,1,2,9,10) AND g.checked_at>=?
          AND NOT EXISTS(SELECT 1 FROM group_account_assignments x WHERE x.conversation_id=g.group_id AND x.account_uid<>g.account_uid)
          AND NOT EXISTS(SELECT 1 FROM public_group_attempts a WHERE a.account_uid=g.account_uid AND a.group_id=g.group_id)
          AND NOT EXISTS(SELECT 1 FROM group_exits e WHERE e.account_uid=g.account_uid AND e.conversation_id=g.group_id)
          AND NOT EXISTS(SELECT 1 FROM monitored_groups m WHERE m.account_uid=g.account_uid AND m.conversation_id=g.group_id AND m.member=1)
          ORDER BY (instr(g.name,'搭子')>0 OR instr(g.name,'组队')>0 OR instr(g.name,'开黑')>0 OR instr(g.name,'一起打瓦')>0) DESC,
          g.checked_at DESC,g.group_id LIMIT 5''',(account,monitor.stamp_after(-3600),monitor.stamp_after(-21600))).fetchall()
        candidates=[row for row in candidates if group_accounts.owns(c,row['group_id'],account)]
        if not candidates:return
    client=client or (client_factory or group_public.Client)(account)
    # Unmet VIP/follow conditions must not consume the whole discovery round.
    # Verify up to five candidates but submit at most one actual application.
    for candidate in candidates:
        if monitor.STOP.is_set():return
        value=client.verify(dict(candidate))
        if value.get('question'):
            with app.LOCKS['live'],app.db() as c:
                value['group_audit_answer']=remember_question(c,account,candidate['group_id'],value['question'])
        reason=eligible(value)
        if reason=='restricted' and group_public.follow_requirement(value):
            target=group_public.follow_requirement(value);retry_proof=None
            with app.db() as c:
                old=c.execute('SELECT * FROM public_group_follows WHERE account_uid=? AND owner_uid=?',(account,target['uid'])).fetchone()
            if follow_needs_recheck(old):
                from group_profiles import profiles
                observed=profiles(account,[target],client=client)
                if len(observed['profiles'])==1 and observed['profiles'][0].get('follow_status')==0:
                    retry_proof=dict(checked_at=app.now(),follow_status=0,proof=observed['proof'])
            with app.LOCKS['live'],app.db() as c:
                cfg=config(c)
                if monitor.STOP.is_set() or not cfg.get('enabled') or cfg.get('account_uid')!=uid_inbox_store._account():return
                follow=prepare_follow(c,account,value,retry_proof)
            if follow:
                try:result=client.follow_owner(value)
                except Exception:
                    proof=getattr(client,'follow_evidence',{})
                    result=dict(status='not_submitted' if proof.get('submission_started') is False else 'uncertain',proof=proof)
                with app.LOCKS['live'],app.db() as c:
                    c.execute('UPDATE public_group_follows SET status=?,proof=? WHERE account_uid=? AND owner_uid=?',
                        (result['status'],json.dumps(dict(follow.get('history',{}),**result['proof'])),account,follow['uid']))
                # Re-read actual conditions; a successful follow is not proof of
                # elapsed follow-days, nor of being admitted to the group.
                if result['status']=='accepted':
                    value=client.verify(dict(candidate))
                    if value.get('question'):
                        with app.LOCKS['live'],app.db() as c:
                            value['group_audit_answer']=remember_question(c,account,candidate['group_id'],value['question'])
                    reason=eligible(value)
            if reason=='restricted':reason='follow_wait'
        elif reason=='restricted' and any(word in unmet_detail(value) for word in ('灯牌','会员','付费')):
            reason='paid'
        if not reason:break
        with app.LOCKS['live'],app.db() as c:
            mark(c,account,candidate['group_id'],reason)
            if reason in ('restricted','follow_wait','paid'):
                c.execute('UPDATE public_group_candidates SET detail=?,checked_at=? WHERE account_uid=? AND group_id=?',
                    (unmet_detail(value),app.now(),account,candidate['group_id']))
    else:return
    with app.LOCKS['live'],app.db() as c:
        cfg=config(c)
        if monitor.STOP.is_set() or not cfg.get('enabled') or cfg.get('account_uid')!=uid_inbox_store._account():return
        # Commit before any submission. A crash or timeout must never cause a second application.
        inserted=c.execute("INSERT OR IGNORE INTO public_group_attempts(account_uid,group_id,created_at,updated_at,status) VALUES(?,?,?,?,'uncertain')",(account,candidate['group_id'],app.now(),app.now())).rowcount
        if not inserted:return
        mark(c,account,candidate['group_id'],'uncertain')
    try:result=client.join(value)
    except Exception:
        # Fixed fields only: never save tickets, cookies or exception text.
        evidence=getattr(client,'join_evidence',{})
        fields=('phase','submission_started','http_status','response_bytes','response_sha256','platform_code','business_code')
        proof={k:evidence[k] for k in fields if k in evidence}
        result=dict(status='not_submitted' if proof.get('submission_started') is False else 'uncertain',proof=proof)
    with app.LOCKS['live'],app.db() as c:
        c.execute('UPDATE public_group_attempts SET status=?,updated_at=?,proof=? WHERE account_uid=? AND group_id=?',
                  (result['status'],app.now(),json.dumps(result['proof']),account,candidate['group_id']))
        mark(c,account,candidate['group_id'],result['status'])
    reconcile(account,reader=catalog_reader)


def tick(*,client_factory=None,catalog_reader=None):
    if monitor.STOP.is_set() or not monitor.GUARD.acquire(blocking=False):return
    acquired=False;account=None
    try:
        account=uid_inbox_store._account()
        with app.db() as c:cfg=config(c)
        if not cfg.get('enabled') or cfg.get('account_uid')!=account or cfg.get('next_run_at','')>app.now():return
        acquired=uid_messaging.GUARD.acquire(blocking=False)
        if not acquired:return
        monitor.ACTIVE=True
        with app.LOCKS['live'],app.db() as c:
            if cfg.get('follow_workflow_version')!=2:
                c.execute("UPDATE public_group_candidates SET status='candidate' WHERE account_uid=? AND status IN ('restricted','follow_wait')",(account,))
                cfg['follow_workflow_version']=2
            cfg.update(next_run_at=monitor.stamp_after(300),detail='正在核验公开群与加入状态');save(c,cfg)
        try:
            run(account,client_factory=client_factory,catalog_reader=catalog_reader)
            detail='本轮筛选完成；等待审核的申请不会重复提交'
        except Exception:detail='本轮公开群读取未完成，稍后重试；未确认的申请不会重发'
        with app.LOCKS['live'],app.db() as c:
            cfg=config(c);cfg['detail']=detail;save(c,cfg)
    finally:
        if acquired:monitor.ACTIVE=False;uid_messaging.GUARD.release()
        monitor.GUARD.release()
