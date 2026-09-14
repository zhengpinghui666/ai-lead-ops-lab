"""User-authorized intent outreach, one durable job per account and user, with bounded rejection retries."""
import hashlib
import json
import threading
import uuid

import clubops as app
import uid_messaging
import demand_freshness
from game_scope import record_exclusion

POLICY_KEY = 'intent_outreach_policy'
RUNTIME_KEY = 'intent_outreach_runtime'
GUARD = threading.Lock()
STOP = threading.Event()
THREAD = None
GREETING_TEMPLATE = '你好{称呼}，想点个陪陪吗？感兴趣可以看看我主业～'
LEGACY_CONTENT = '点陪🥣看我主业'


def evidence_game(c, evidence):
    """Bind copy to the chosen demand, not the user's latest unrelated comment."""
    import monitoring
    import semantic
    kind = evidence.get('evidence_type') or next((k for k in ('comment', 'live', 'group') if k+'_id' in evidence), None)
    rid = evidence.get(kind+'_id', evidence.get('id')) if kind else None
    if kind == 'comment':
        row = c.execute('SELECT raw_text FROM comments WHERE id=?', (rid,)).fetchone()
        return monitoring.observation_analysis(c, rid, row['raw_text'], semantic.state()['engine']).get('game', '') if row else ''
    if kind == 'live':
        import live_workflow
        row = c.execute(live_workflow.SELECT+' WHERE m.id=?', (rid,)).fetchone()
        return live_workflow.project(c, row, history=False).get('game', '') if row else ''
    if kind == 'group':
        import group_monitor
        row = c.execute(group_monitor.SELECT+' WHERE m.id=?', (rid,)).fetchone()
        return group_monitor.project(c, row).get('game', '') if row else ''
    return ''


def game_allowed(game, policy):
    # Unknown-game outreach needs the approved explicit Valorant greeting.
    return game == app.TARGET_GAME or (not game and policy.get('template') == 'public_gender_greeting_v1')


def rendered_content(c, policy, recipient, *, game=None):
    if policy.get('template') != 'public_gender_greeting_v1':
        return policy['content']
    # Only an explicit profile field collected with a numeric UID is usable.
    # Missing, hidden, stale or conflicting values use a neutral greeting.
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
    rows = c.execute('''SELECT p.profile_gender,p.profile_gender_observed_at FROM people p
        JOIN sources s ON s.id=p.source_id WHERE p.external_id=? AND s.kind='browser'
        AND p.profile_gender_observed_at>=? ORDER BY p.profile_gender_observed_at DESC''',
        (recipient, since)).fetchall()
    genders = {r['profile_gender'] for r in rows if r['profile_gender_observed_at']==rows[0]['profile_gender_observed_at']} if rows else set()
    gender = next(iter(genders)) if len(genders)==1 else 0
    template = GREETING_TEMPLATE if game == app.TARGET_GAME else GREETING_TEMPLATE.replace('陪陪', '瓦陪陪')
    return template.replace('{称呼}', {1:'小哥哥', 2:'小姐姐'}.get(gender, '呀'))


def replace_greeting(instruction):
    """Local operator change: retain sender, exclusions and unrelated policy flags."""
    if not isinstance(instruction,str) or not 1<=len(instruction.strip())<=1000:
        raise ValueError('需要明确的文案替换与回退授权')
    with GUARD, app.LOCKS['live'], app.db() as c:
        policy = read(c, POLICY_KEY)
        if not policy or policy.get('content') != LEGACY_CONTENT or policy.get('template'):
            raise ValueError('当前文案与预期旧版不一致；未覆盖配置')
        previous_revision = policy['revision']
        policy.update(content=GREETING_TEMPLATE, template='public_gender_greeting_v1',
            instruction=instruction.strip(), granted_at=app.now(), revision=str(uuid.uuid4()),
            content_fallback=dict(content=LEGACY_CONTENT, status='armed', previous_revision=previous_revision))
        write(c, POLICY_KEY, policy)
        app.event(c,'intent_outreach','新开场模板已启用；明确内容拒绝或风控时回退旧版，失败对象不换文案补发')
    return state()


def rejection_kind(result):
    """Classify only a definite, correlated send rejection; never guess from a generic code."""
    e = result.get('evidence', {})
    if (result.get('status')!='failed' or e.get('phase')!='send'
            or e.get('submission_reserved') is not True or e.get('server_message_id')):
        return None
    if e.get('http_status')==429:
        return 'rate_limit'
    if e.get('http_status') not in (200,403) or e.get('platform_reason_code')=='7173':
        return None
    text = e.get('platform_message','')
    if not isinstance(text,str):return None
    if any(word in text for word in ('风控','风险','账号异常','帐号异常','发送频繁','操作频繁','发送过于频繁','操作过于频繁','发送太频繁','私信功能已被限制','私信功能被封禁')):
        return 'account_risk'
    if any(word in text for word in ('敏感词','敏感内容','内容违规','违规内容','内容不符合','内容违反','不当内容','违禁内容')):
        return 'content_rejected'
    return None


def rollback_rejected_template(c, policy):
    """Also handles a restart between storing a rejected receipt and rolling back."""
    if policy.get('content_fallback',{}).get('status')!='armed':return None
    for raw in c.execute('''SELECT a.* FROM uid_message_attempts a JOIN message_jobs j ON j.id=a.job_id
        WHERE a.sender_uid=? AND j.request_id LIKE 'intent-outreach-v1-%' AND a.status='failed'
        ORDER BY a.updated_at DESC''', (policy['sender_uid'],)):
        evidence = json.loads(raw['evidence'])
        if evidence.get('operator_authorization',{}).get('policy_revision')!=policy['revision']:continue
        kind = rejection_kind(dict(status=raw['status'],evidence=evidence))
        if not kind:continue
        fallback = dict(policy['content_fallback'], status='rolled_back', reason=kind,
            job_id=raw['job_id'], at=app.now(), rejected_revision=policy['revision'])
        policy.update(content=fallback['content'], template=None, revision=str(uuid.uuid4()), content_fallback=fallback)
        write(c,POLICY_KEY,policy)
        detail = '新文案收到明确内容拒绝，已回退旧版；该对象不补发'
        if kind!='content_rejected':detail='平台限制了发送，已回退旧版并暂停自动私信，等待核对限制'
        write(c,RUNTIME_KEY,dict(status='waiting' if kind=='content_rejected' else 'attention',
            checked_at=app.now(),last_job_id=raw['job_id'],detail=detail))
        app.event(c,'intent_outreach',detail+'；发送记录 #'+str(raw['job_id']))
        return kind
    return None


def read(c, key):
    row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else {}


def write(c, key, value):
    c.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))


def authorize(content, instruction, sender_uid):
    """Local setup only, after the operator explicitly authorizes real sends."""
    import uid_protocol
    uid_protocol.numeric_uid(sender_uid)
    if not isinstance(content, str) or not 1 <= len(content.strip()) <= 1000 or not instruction.strip():
        raise ValueError('需要明确的发送文案与操作授权')
    policy = dict(enabled=True, content=content, instruction=instruction, sender_uid=sender_uid,
                  granted_at=app.now(), revision=str(uuid.uuid4()))
    with GUARD, app.LOCKS['live'], app.db() as c:
        write(c, POLICY_KEY, policy)
        write(c, RUNTIME_KEY, dict(status='waiting', detail='等待新的意向用户', checked_at=None))
        app.event(c, 'intent_outreach', '已保存自动私信操作授权；同一账号对同一用户只尝试一次')
    return state()


def control(enabled, mode='live'):
    if mode != 'live' or type(enabled) is not bool:
        raise ValueError('自动私信只支持正式工作区')
    with app.LOCKS['live'], app.db() as c:
        policy = read(c, POLICY_KEY)
        if not policy:
            raise ValueError('尚未保存明确的自动发送授权')
        policy['enabled'] = enabled
        write(c, POLICY_KEY, policy)
        write(c, RUNTIME_KEY, dict(status='waiting' if enabled else 'paused',
            detail='等待新的意向用户' if enabled else '自动私信已关闭', checked_at=app.now()))
    return state(mode)


def authorize_retries(instruction):
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError('需要明确的重试授权')
    with GUARD, app.LOCKS['live'], app.db() as c:
        policy = read(c, POLICY_KEY)
        if not policy:raise ValueError('尚未配置自动私信')
        policy.update(retry_rejected=True,retry_instruction=instruction,retry_granted_at=app.now())
        write(c, POLICY_KEY, policy)
        app.event(c,'intent_outreach','已授权明确拒绝后的有限重试；已知互关限制等待关系条件改变')
    return state()


def state(mode='live'):
    if mode != 'live':
        return dict(enabled=False, configured=False, status='disabled', counts={})
    with app.db() as c:
        policy, runtime = read(c, POLICY_KEY), read(c, RUNTIME_KEY)
        counts = dict(c.execute("SELECT a.status,COUNT(*) FROM uid_message_attempts a JOIN message_jobs j ON j.id=a.job_id WHERE j.request_id LIKE 'intent-outreach-v1-%' GROUP BY a.status").fetchall())
    return dict(configured=bool(policy), enabled=bool(policy.get('enabled')), content=policy.get('content', ''),
        retry_rejected=bool(policy.get('retry_rejected')), max_retries=2,
        status=runtime.get('status', 'paused'), detail=runtime.get('detail', ''), counts=counts,
        checked_at=runtime.get('checked_at'), last_job_id=runtime.get('last_job_id'),
        template=policy.get('template'), content_fallback=policy.get('content_fallback',{}))


def retry_candidate(c, policy):
    """Return a due definite rejection, or a global rate-limit cooldown."""
    from datetime import datetime,timezone
    if not policy.get('retry_rejected'):return None
    due=[]
    for raw in c.execute("""SELECT a.* FROM uid_message_attempts a JOIN message_jobs j ON j.id=a.job_id
            WHERE j.request_id LIKE 'intent-outreach-v1-%' AND a.sender_uid=? AND a.status='failed' AND a.phase='send'
            ORDER BY a.updated_at,a.job_id""",(policy['sender_uid'],)):
        evidence=json.loads(raw['evidence'])
        if rejection_kind(dict(status=raw['status'],evidence=evidence)):continue
        if evidence.get('platform_reason_code')=='7173':continue
        if evidence.get('http_status') not in (200,429) or evidence.get('server_message_id') or evidence.get('submission_reserved') is not True:continue
        retries=len(evidence.get('delivery_history',[]))
        if retries>=2:continue
        elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(raw['updated_at'])).total_seconds()
        delay=900 if evidence['http_status']==429 else 60 if retries==0 else 300
        if elapsed<delay:
            if evidence['http_status']==429:return {'cooldown':True}
            continue
        grant=evidence.get('operator_authorization')
        if not grant:continue
        grant={**grant,'policy_revision':policy['revision']}
        job,person,source=uid_messaging.snapshot(c,raw['job_id'])
        settings,issues=uid_messaging.config(authorized_recipient=person['external_id'])
        if issues:continue
        try:uid_messaging.conditions(job,person,source,settings,authorization=grant,connection=c)
        except ValueError:continue
        due.append(dict(job=job,grant=grant))
    return due[0] if due else None


def contact_accounts(policy):
    """A replacement account carries the previous contact exclusions, not receipts."""
    import uid_protocol
    previous=policy.get('prior_sender_uids',[])
    if not isinstance(previous,list) or len(previous)>100:raise ValueError('换号去重范围无效')
    return list(dict.fromkeys(uid_protocol.numeric_uid(v) for v in [policy['sender_uid'],*previous]))


def previously_contacted(c,policy,recipient):
    accounts=[a for a in contact_accounts(policy) if a!=policy['sender_uid']]
    if not accounts:return False
    slots=','.join('?' for _ in accounts)
    return bool(c.execute(f'SELECT 1 FROM uid_message_attempts WHERE sender_uid IN ({slots}) AND recipient_uid=? LIMIT 1',(*accounts,recipient)).fetchone())


def candidate(c, policy):
    import monitoring
    import semantic
    import live_workflow
    engine = semantic.state()['engine']
    accounts=contact_accounts(policy)
    slots=','.join('?' for _ in accounts)
    eligible = f"""p.do_not_contact=0 AND p.external_id NOT IN ({slots}) AND NOT EXISTS
        (SELECT 1 FROM uid_message_attempts a WHERE a.sender_uid IN ({slots}) AND a.recipient_uid=p.external_id)"""
    args = tuple(accounts+accounts)
    import group_monitor
    for raw in c.execute(group_monitor.SELECT + " JOIN people eligible_person ON eligible_person.id=m.person_id WHERE "
            + eligible.replace('p.', 'eligible_person.') + ' ORDER BY m.id DESC', args):
        if group_monitor.eligible(c,raw,engine,policy['sender_uid'],allow_unknown=game_allowed('', policy)):
            return dict(raw,recipient_uid=raw['uid'],evidence_type='group',outreach_game=group_monitor.project(c,raw,engine=engine).get('game',''))
    for row in c.execute('''SELECT x.*,p.external_id AS recipient_uid,l.id AS lead_id
            FROM comments x JOIN people p ON p.id=x.person_id JOIN leads l ON l.person_id=p.id
            JOIN sources s ON s.id=p.source_id WHERE s.kind='browser' AND ''' + eligible + ' ORDER BY x.id DESC', args):
        if record_exclusion(c,'comment',row['id']):continue
        if not demand_freshness.assess(row['published_at'])['eligible']:continue
        analysis = monitoring.observation_analysis(c, row['id'], row['raw_text'], engine)
        if analysis.get('category') == 'buyer' and game_allowed(analysis.get('game'), policy) and analysis.get('analysis_method') in ('model', 'human'):
            return dict(row, evidence_type='comment', outreach_game=analysis.get('game',''))
    ids = [r[0] for r in c.execute('''SELECT m.id FROM live_messages m JOIN live_links k ON k.message_id=m.id
        JOIN people p ON p.id=k.person_id WHERE m.filter_reason='' AND ''' + eligible + ' ORDER BY m.id DESC', args)]
    for rid in ids:
        if record_exclusion(c,'live',rid):continue
        if not demand_freshness.record(c,'live',rid)['eligible']:continue
        row = live_workflow.project(c, c.execute(live_workflow.SELECT + ' WHERE m.id=?', (rid,)).fetchone(), history=False, model_engine=engine)
        if row['category'] == 'buyer' and game_allowed(row.get('game'), policy) and row['analysis_method'] in ('model', 'human'):
            return dict(row, recipient_uid=row['uid'], evidence_type='live', outreach_game=row.get('game',''))
    return None


def prepare_draft(row, policy, content):
    request_id = 'intent-outreach-v1-'+policy['sender_uid']+'-'+row['recipient_uid']
    with app.LOCKS['live'], app.db() as c:
        old = c.execute('''SELECT j.*,p.external_id AS recipient FROM message_jobs j
            JOIN leads l ON l.id=j.lead_id JOIN people p ON p.id=l.person_id WHERE j.request_id=?''', (request_id,)).fetchone()
        if old and old['status'] == 'draft' and old['recipient'] == row['recipient_uid'] and not c.execute(
                'SELECT 1 FROM uid_message_attempts WHERE job_id=?', (old['id'],)).fetchone():
            if old['content'] != content or old['lead_id'] != row['lead_id']:
                c.execute('UPDATE message_jobs SET content=?,lead_id=?,updated_at=? WHERE id=?', (content,row['lead_id'],app.now(),old['id']))
                app.event(c,'intent_outreach','未提交草稿已按当前需求更新文案；发送记录 #'+str(old['id']))
            return dict(c.execute('SELECT * FROM message_jobs WHERE id=?', (old['id'],)).fetchone())
    return app.mutate('draft',dict(lead_id=row['lead_id'],request_id=request_id,content=content))


def tick():
    import collection_accounts
    with app.db() as c:
        policy=read(c,POLICY_KEY)
        if c.execute('SELECT 1 FROM collection_accounts').fetchone():
            assigned=collection_accounts.role_accounts(c,'outreach')
            if not any(a['sender_uid']==policy.get('sender_uid') for a in assigned):return
    import uid_session_renewal
    if uid_session_renewal.pending():
        return
    if STOP.is_set() or not GUARD.acquire(blocking=False):
        return
    try:
        if uid_session_renewal.pending():
            return
        with app.LOCKS['live'], app.db() as c:
            policy = read(c, POLICY_KEY)
            if rollback_rejected_template(c,policy):return
        with app.db() as c:
            policy, runtime = read(c, POLICY_KEY), read(c, RUNTIME_KEY)
            if not policy.get('enabled') or runtime.get('status') == 'attention':
                return
            retry = retry_candidate(c, policy)
            if retry and retry.get('cooldown'):return
            row = None if retry else candidate(c, policy)
            content = rendered_content(c,policy,row['recipient_uid'],game=row['outreach_game']) if row else None
        if (not row and not retry) or STOP.is_set():
            return
        if retry:
            job,grant=retry['job'],retry['grant']
        else:
            job = prepare_draft(row, policy, content)
            kind = row['evidence_type']
            grant = dict(job_id=job['id'], sender_uid=policy['sender_uid'], recipient_uid=row['recipient_uid'],
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                granted_at=policy['granted_at'], instruction=policy['instruction'], policy_revision=policy['revision'])
            grant[kind+'_id'] = row['id']
            grant[kind+'_sha256'] = hashlib.sha256(row['raw_text'].encode()).hexdigest()
        try:
            result = uid_messaging.send_one(job['id'], operator_authorization=grant,
                retry_note=policy['retry_instruction'] if retry else None)
        except uid_messaging.ChannelBusy:
            # No platform attempt was registered. Keep the same draft and let
            # the normal bounded loop retry after the channel owner releases it.
            with app.LOCKS['live'], app.db() as c:
                write(c, RUNTIME_KEY, dict(status='waiting', checked_at=app.now(), last_job_id=job['id'],
                    detail='私信通道正在处理其他任务，等待下一次发送检查'))
            return
        except demand_freshness.FreshnessError as exc:
            # The source can expire between selection and preparation. This is
            # a per-lead stop, not a channel failure or permission to use old text.
            with app.LOCKS['live'], app.db() as c:
                c.execute("UPDATE message_jobs SET status='blocked',detail=?,updated_at=? WHERE id=? AND status='draft'",
                    (str(exc), app.now(), job['id']))
                write(c, RUNTIME_KEY, dict(status='waiting', checked_at=app.now(), last_job_id=job['id'], detail=str(exc)))
            return
        evidence = result.get('evidence', {})
        attention = not evidence.get('demand_freshness') and (result['status'] in ('unknown', 'not_connected')
            or evidence.get('http_status') in (401, 403)
            or evidence.get('http_status')==429 and (not policy.get('retry_rejected') or len(evidence.get('delivery_history',[]))>=2)
            or result['status'] == 'failed' and evidence.get('phase') != 'send')
        with app.LOCKS['live'], app.db() as c:
            current_policy=read(c,POLICY_KEY)
            if rollback_rejected_template(c,current_policy):return
            write(c, RUNTIME_KEY, dict(status='attention' if attention else 'waiting', checked_at=app.now(),
                last_job_id=job['id'], detail='发送通道需要处理，请查看发送记录' if attention else '等待新的意向用户'))
    finally:
        GUARD.release()


def start_service():
    global THREAD
    if THREAD and THREAD.is_alive():
        return
    STOP.clear()
    def loop():
        while not STOP.wait(15):
            try:
                tick()
            except Exception as exc:
                with app.LOCKS['live'], app.db() as c:
                    write(c, RUNTIME_KEY, dict(status='attention', checked_at=app.now(),
                        error_type=type(exc).__name__, detail='自动私信处理未完成，请查看记录；不会重发已有尝试'))
                    app.event(c, 'intent-outreach-error', '自动私信处理异常：'+type(exc).__name__)
    THREAD = threading.Thread(target=loop, name='intent-outreach', daemon=True)
    THREAD.start()


def shutdown():
    STOP.set()
    if THREAD:
        THREAD.join()
