"""User-authorized intent outreach, one durable job per account and user, with bounded rejection retries."""
import hashlib
import json
import threading
import uuid

import clubops as app
import uid_messaging

POLICY_KEY = 'intent_outreach_policy'
RUNTIME_KEY = 'intent_outreach_runtime'
GUARD = threading.Lock()
STOP = threading.Event()
THREAD = None


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
        checked_at=runtime.get('checked_at'), last_job_id=runtime.get('last_job_id'))


def retry_candidate(c, policy):
    """Return a due definite rejection, or a global rate-limit cooldown."""
    from datetime import datetime,timezone
    if not policy.get('retry_rejected'):return None
    due=[]
    for raw in c.execute("""SELECT a.* FROM uid_message_attempts a JOIN message_jobs j ON j.id=a.job_id
            WHERE j.request_id LIKE 'intent-outreach-v1-%' AND a.sender_uid=? AND a.status='failed' AND a.phase='send'
            ORDER BY a.updated_at,a.job_id""",(policy['sender_uid'],)):
        evidence=json.loads(raw['evidence'])
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


def candidate(c, policy):
    import monitoring
    import semantic
    import live_workflow
    engine = semantic.state()['engine']
    eligible = """p.do_not_contact=0 AND p.external_id<>? AND NOT EXISTS
        (SELECT 1 FROM uid_message_attempts a WHERE a.sender_uid=? AND a.recipient_uid=p.external_id)"""
    args = (policy['sender_uid'], policy['sender_uid'])
    import group_monitor
    for raw in c.execute(group_monitor.SELECT + " JOIN people eligible_person ON eligible_person.id=m.person_id WHERE "
            + eligible.replace('p.', 'eligible_person.') + ' ORDER BY m.id DESC', args):
        if group_monitor.eligible(c,raw,engine,policy['sender_uid']):
            return dict(raw,recipient_uid=raw['uid'],evidence_type='group')
    for row in c.execute('''SELECT x.*,p.external_id AS recipient_uid,l.id AS lead_id
            FROM comments x JOIN people p ON p.id=x.person_id JOIN leads l ON l.person_id=p.id
            JOIN sources s ON s.id=p.source_id WHERE s.kind='browser' AND ''' + eligible + ' ORDER BY x.id DESC', args):
        analysis = monitoring.observation_analysis(c, row['id'], row['raw_text'], engine)
        if analysis.get('category') == 'buyer' and analysis.get('analysis_method') in ('model', 'human'):
            return dict(row, evidence_type='comment')
    ids = [r[0] for r in c.execute('''SELECT m.id FROM live_messages m JOIN live_links k ON k.message_id=m.id
        JOIN people p ON p.id=k.person_id WHERE m.filter_reason='' AND ''' + eligible + ' ORDER BY m.id DESC', args)]
    for rid in ids:
        row = live_workflow.project(c, c.execute(live_workflow.SELECT + ' WHERE m.id=?', (rid,)).fetchone(), history=False, model_engine=engine)
        if row['category'] == 'buyer' and row['analysis_method'] in ('model', 'human'):
            return dict(row, recipient_uid=row['uid'], evidence_type='live')
    return None


def tick():
    if STOP.is_set() or not GUARD.acquire(blocking=False):
        return
    try:
        with app.db() as c:
            policy, runtime = read(c, POLICY_KEY), read(c, RUNTIME_KEY)
            if not policy.get('enabled') or runtime.get('status') == 'attention':
                return
            retry = retry_candidate(c, policy)
            if retry and retry.get('cooldown'):return
            row = None if retry else candidate(c, policy)
        if (not row and not retry) or STOP.is_set():
            return
        if retry:
            job,grant=retry['job'],retry['grant']
        else:
            job = app.mutate('draft', dict(lead_id=row['lead_id'],
                request_id='intent-outreach-v1-'+row['recipient_uid'], content=policy['content']))
            kind = row['evidence_type']
            grant = dict(job_id=job['id'], sender_uid=policy['sender_uid'], recipient_uid=row['recipient_uid'],
                content_sha256=hashlib.sha256(policy['content'].encode()).hexdigest(),
                granted_at=policy['granted_at'], instruction=policy['instruction'], policy_revision=policy['revision'])
            grant[kind+'_id'] = row['id']
            grant[kind+'_sha256'] = hashlib.sha256(row['raw_text'].encode()).hexdigest()
        result = uid_messaging.send_one(job['id'], operator_authorization=grant,
            retry_note=policy['retry_instruction'] if retry else None)
        evidence = result.get('evidence', {})
        attention = (result['status'] in ('unknown', 'not_connected')
            or evidence.get('http_status') in (401, 403)
            or evidence.get('http_status')==429 and (not policy.get('retry_rejected') or len(evidence.get('delivery_history',[]))>=2)
            or result['status'] == 'failed' and evidence.get('phase') != 'send')
        with app.LOCKS['live'], app.db() as c:
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
            except Exception:
                with app.LOCKS['live'], app.db() as c:
                    write(c, RUNTIME_KEY, dict(status='attention', checked_at=app.now(),
                        detail='自动私信处理未完成，请查看记录；不会重发已有尝试'))
    THREAD = threading.Thread(target=loop, name='intent-outreach', daemon=True)
    THREAD.start()


def shutdown():
    STOP.set()
    if THREAD:
        THREAD.join()
