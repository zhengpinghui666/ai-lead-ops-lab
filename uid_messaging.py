"""ClubOps job integration for the experimental personal-account HTTP channel."""
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

import clubops as app
import uid_protocol
import uid_transport
import demand_freshness

CONFIG_FILE = 'uid-http.json'
GUARD = threading.Lock()
RETRYABLE = {'draft', 'blocked', 'not_connected'}  # No network attempt exists yet.
SCHEMA = '''
CREATE TABLE IF NOT EXISTS uid_message_attempts (
 job_id INTEGER PRIMARY KEY REFERENCES message_jobs(id),
 dedupe_key TEXT NOT NULL UNIQUE, sender_uid TEXT NOT NULL, recipient_uid TEXT NOT NULL,
 client_message_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, phase TEXT NOT NULL,
 detail TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
'''


def config(*, authorized_recipient=None):
    issues, value = [], {}
    path = app.DATA_DIR / CONFIG_FILE
    try:
        if path.exists():
            with path.open('rb') as stream:
                raw = stream.read(32769)
            if len(raw) > 32768:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) - {'enabled', 'sender_uid', 'allowed_recipient_uids', 'provider_file'}:
                raise ValueError()
    except (OSError, ValueError, TypeError):
        return {}, ['本地 HTTP 配置无法读取或格式无效']
    # A validated local job grant supplies its own single-recipient scope.
    # Public routes never supply this argument; their explicit allowlist stays.
    if authorized_recipient is not None:
        value['allowed_recipient_uids'] = [uid_protocol.numeric_uid(authorized_recipient)]
    if value.get('enabled') is not True:
        issues.append('个人号 HTTP 通道未启用')
    try:
        uid_protocol.numeric_uid(value.get('sender_uid'))
    except ValueError:
        issues.append('缺少有效的发送方数字 UID 字符串')
    try:
        peers = value.get('allowed_recipient_uids', [])
        if not isinstance(peers, list) or not 1 <= len(peers) <= 20:
            raise ValueError()
        for peer in peers:
            uid_protocol.numeric_uid(peer)
        if value.get('sender_uid') in peers or len(set(peers)) != len(peers):
            raise ValueError()
    except (ValueError, TypeError):
        issues.append('需配置 1–20 个不同于发送方、已同意测试的数字 UID 字符串')
    try:
        provider = value.get('provider_file', '')
        if not isinstance(provider, str) or not provider:
            issues.append('缺少当前账号的本机会话与逐请求签名提供器')
        else:
            target = Path(provider)
            if not target.is_absolute():
                target = app.DATA_DIR / target
            if not target.is_file() or target.suffix != '.py':
                issues.append('本机会话提供器文件不存在')
            value['provider_file'] = str(target.resolve())
    except (OSError, ValueError, TypeError):
        issues.append('本机会话提供器路径无效')
    if value.get('provider_file') == str(Path(__file__).with_name('uid_session.py').resolve()):
        import uid_session
        status = uid_session.local_status(app.DATA_DIR)
        if not status['im_read_verified']:
            detail = '本机 IM 会话最近一次核对未通过或尚未核对'
            if status['platform_code'].isascii() and status['platform_code'].isdigit():
                detail += '（业务码 ' + status['platform_code'] + '）'
            issues.append(detail + '；发送通道保持关闭')
    return value, issues


def state(mode='live'):
    if mode != 'live':
        return {'status': 'disabled', 'can_attempt': False, 'transport': 'http', 'issues': ['演示区不连接真实 HTTP 通道']}
    import uid_session_renewal
    value, issues = config()
    try:
        sender = uid_protocol.numeric_uid(value.get('sender_uid'))
    except ValueError:
        sender = ''
    return dict(status='not_configured' if issues else 'configured_unverified', transport='http',
                can_attempt=not issues, sender_uid=sender,
                allowed_recipients=len(value.get('allowed_recipient_uids', [])) if isinstance(value.get('allowed_recipient_uids'), list) else 0,
                live_verified=False, issues=issues, session_renewal=uid_session_renewal.state(),
                detail='纯 HTTP 通道已有一条授权测试的服务端接受证据；当前配置仍须核对，配置就绪不等于发送成功或送达。')


def probe_identity(mode='live'):
    """Explicit, non-sending diagnostic. No DB writes or cached send permission."""
    if mode != 'live':
        raise ValueError('演示区不连接真实 HTTP 通道')
    if not GUARD.acquire(blocking=False):
        raise ValueError('已有 HTTP 核对或发送正在处理；请等待完成')
    try:
        settings, issues = config()
        if issues:
            return dict(status='not_configured', phase='identity', transport='http',
                        can_send=False, live_verified=False, checked_at=app.now(),
                        detail='；'.join(issues))
        return dict(uid_transport.verify_identity(settings), checked_at=app.now())
    finally:
        GUARD.release()


def snapshot(c, job_id):
    job = app.required(c, 'message_jobs', job_id)
    lead = app.required(c, 'leads', job['lead_id'])
    person = app.required(c, 'people', lead['person_id'])
    source = app.required(c, 'sources', person['source_id'])
    return dict(job), dict(person), dict(source)


def authorized_outreach(c, authorization, job, person, settings):
    """Validate a local operator grant without inventing recipient consent."""
    import monitoring
    import semantic
    fields = {'job_id', 'sender_uid', 'recipient_uid', 'content_sha256', 'granted_at', 'instruction'}
    comment_fields, live_fields = {'comment_id', 'comment_sha256'}, {'live_id', 'live_sha256'}
    group_fields = {'group_id','group_sha256'}
    keys = set(authorization) - {'policy_revision'} if isinstance(authorization, dict) else set()
    if not isinstance(authorization, dict) or keys not in (fields | comment_fields, fields | live_fields, fields | group_fields):
        raise ValueError('本次操作授权格式无效；未发送')
    if 'policy_revision' in authorization:
        import intent_outreach
        policy = intent_outreach.read(c, intent_outreach.POLICY_KEY)
        if (not policy.get('enabled') or policy.get('revision') != authorization['policy_revision']
                or policy.get('sender_uid') != settings.get('sender_uid') or policy.get('content') != job['content']):
            raise ValueError('自动发送授权已关闭或变更；未发送')
    if (authorization['job_id'] != job['id']
            or authorization['sender_uid'] != settings.get('sender_uid')
            or authorization['recipient_uid'] != person['external_id']
            or authorization['content_sha256'] != hashlib.sha256(job['content'].encode()).hexdigest()
            or not isinstance(authorization['instruction'], str)
            or not 1 <= len(authorization['instruction'].strip()) <= 1000
            or not isinstance(authorization['granted_at'], str)
            or not authorization['granted_at'].strip()):
        raise ValueError('本次操作授权与任务、账号、收件人或文案不符；未发送')
    if 'group_id' in authorization:
        demand_freshness.require(c, 'group', authorization['group_id'])
        import group_monitor
        raw=c.execute(group_monitor.SELECT+' WHERE m.id=?',(authorization['group_id'],)).fetchone()
        if (not raw or raw['person_id']!=person['id']
                or authorization['group_sha256']!=hashlib.sha256(raw['raw_text'].encode()).hexdigest()
                or not group_monitor.eligible(c,raw,semantic.state()['engine'],settings['sender_uid'])):
            raise ValueError('群消息未通过初筛和当前模型确认，或群监控授权已失效；未发送')
        return
    from game_scope import record_exclusion
    kind='live' if 'live_id' in authorization else 'comment'
    excluded=record_exclusion(c,kind,authorization[kind+'_id'])
    if excluded:raise ValueError(excluded+' 未发送')
    demand_freshness.require(c, kind, authorization[kind+'_id'])
    if 'live_id' in authorization:
        import live_workflow
        raw = c.execute(live_workflow.SELECT + ' WHERE m.id=?', (authorization['live_id'],)).fetchone()
        if (not raw or raw['person_id'] != person['id'] or raw['filter_reason']
                or authorization['live_sha256'] != hashlib.sha256(raw['raw_text'].encode()).hexdigest()):
            raise ValueError('本次操作授权缺少对应弹幕证据；未发送')
        analysis = live_workflow.project(c, raw, history=False)
        if analysis.get('category') != 'buyer' or analysis.get('analysis_method') not in ('rules', 'model', 'human'):
            raise ValueError('该弹幕当前未被识别为有意向；未发送')
        return
    comment = c.execute('SELECT * FROM comments WHERE id=?', (authorization['comment_id'],)).fetchone()
    if (not comment or comment['person_id'] != person['id']
            or authorization['comment_sha256'] != hashlib.sha256(comment['raw_text'].encode()).hexdigest()):
        raise ValueError('本次操作授权缺少对应评论证据；未发送')
    analysis = monitoring.observation_analysis(c, comment['id'], comment['raw_text'], semantic.state()['engine'])
    if analysis.get('category') != 'buyer' or analysis.get('analysis_method') not in ('rules', 'model', 'human'):
        raise ValueError('该评论当前未被识别为有意向；未发送')


def conditions(job, person, source, settings, *, authorization=None, connection=None):
    if person['do_not_contact']:
        raise ValueError('该用户已禁止联系；未发送')
    if authorization is not None:
        if connection is None:
            raise ValueError('缺少本次授权核对上下文；未发送')
        authorized_outreach(connection, authorization, job, person, settings)
    elif person['contact_basis'] not in ('inbound', 'opt_in') or not person['contact_note'].strip():
        raise ValueError('缺少联系依据或已禁止联系；未发送')
    uid = uid_protocol.numeric_uid(person['external_id'])
    if source['kind'] not in ('browser', 'uid_test'):
        raise ValueError('来源未明确记录数字 UID；不能将导入的 OpenID 或抖音号用于发送')
    if uid not in settings.get('allowed_recipient_uids', []):
        raise ValueError('接收方不在本次授权发送范围内；未发送')
    if uid == settings.get('sender_uid'):
        raise ValueError('发送方与接收方不能相同')
    for completed in ('CO-DM-01', 'CO-HTTP-02'):
        if completed in job['content']:
            raise ValueError(completed + ' 已执行过连通测试，禁止重复发送')
    if not job['content'].strip() or len(job['content']) > 1000:
        raise ValueError('本通道仅支持 1–1000 字的单条文字消息')
    return uid


def register_target(body, mode='live'):
    if mode != 'live':
        raise ValueError('请在正式工作区登记明确同意的测试对象')
    uid = uid_protocol.numeric_uid(body.get('uid'))
    note = app.clean(body.get('contact_note'))
    if not note:
        raise ValueError('请填写该测试对象同意接收测试消息的依据')
    # This records a separate contact, never invents a comment or paid lead.
    with app.LOCKS[mode], app.db(mode) as c:
        source = c.execute("SELECT id FROM sources WHERE kind='uid_test' LIMIT 1").fetchone()
        source_id = source['id'] if source else c.execute("INSERT INTO sources(name,kind,status,notes) VALUES('HTTP 私信授权测试对象','uid_test','local','用户登记的测试范围；服务端会话身份仍须再次核对')").lastrowid
        existing = c.execute('SELECT * FROM people WHERE source_id=? AND external_id=?', (source_id, uid)).fetchone()
        if existing:
            raise ValueError('该测试对象已登记；请从会话列表查看，不覆盖已有禁止联系记录')
        person_id = c.execute("INSERT INTO people(source_id,external_id,nickname,contact_basis,contact_note) VALUES(?,?,?,'opt_in',?)", (source_id, uid, app.clean(body.get('nickname'), 120) or '测试对象 ' + uid, note)).lastrowid
        lead_id = c.execute('INSERT INTO leads(person_id,updated_at) VALUES(?,?)', (person_id, app.now())).lastrowid
        app.event(c, 'uid_target', f'登记授权测试对象 #{lead_id}，未采集或发送消息')
    return {'lead_id': lead_id}


def send_one(job_id, mode='live', *, transport=None, resume_note=None, operator_authorization=None, retry_note=None):
    """Resume is an explicit local maintenance action, never an automatic retry.

    Only a proven pre-submission failure can continue, with the same immutable
    sender, recipient, content, dedupe key and client ID. HTTP routes do not
    expose resume_note or retry_note. Explicit platform rejections may be retried
    at most twice with backoff and a preserved receipt/client-ID history.
    Accepted and uncertain submissions never become retryable.
    """
    if mode != 'live':
        raise ValueError('演示区不允许个人号 HTTP 发送')
    if not GUARD.acquire(blocking=False):
        raise ValueError('已有一条 HTTP 任务正在处理；请等待完成')
    try:
        authorization = json.loads(json.dumps(operator_authorization)) if operator_authorization is not None else None
        return _send_one(job_id, transport or uid_transport.send, resume_note, authorization, retry_note)
    finally:
        GUARD.release()


def _send_one(job_id, transport, resume_note=None, authorization=None, retry_note=None):
    if resume_note is not None and (not isinstance(resume_note, str) or not resume_note.strip() or len(resume_note) > 500):
        raise ValueError('继续准备需要明确的本地处理依据')
    if retry_note is not None and (resume_note is not None or not isinstance(retry_note, str) or not retry_note.strip() or len(retry_note)>500):
        raise ValueError('重试需要明确的操作授权')
    authorized_uid = authorization.get('recipient_uid') if isinstance(authorization, dict) else None
    settings, issues = config(authorized_recipient=authorized_uid)
    history, delivery_history = [], []
    with app.LOCKS['live'], app.db() as c:
        c.execute('BEGIN IMMEDIATE')
        job, person, source = snapshot(c, job_id)
        old = c.execute('SELECT * FROM uid_message_attempts WHERE job_id=?', (job_id,)).fetchone()
        if old and resume_note is None and retry_note is None:
            if old['status'] == 'submitting':
                # GUARD was acquired by this call, so no local attempt is active.
                # A lost final DB write is uncertainty, never permission to retry.
                return {'status': 'unknown', 'detail': '先前尝试没有完整结果；请核对会话，不会重发'}
            return {key: old[key] for key in ('status', 'detail')}
        if retry_note is not None:
            if not old or old['status'] != 'failed' or old['phase'] != 'send' or job['status'] != 'failed':
                raise ValueError('只有平台明确拒绝的消息可以重试；接受或不确定的结果不重发')
            previous = json.loads(old['evidence'])
            if previous.get('http_status') not in (200,429) or previous.get('server_message_id') or previous.get('submission_reserved') is not True:
                raise ValueError('缺少平台明确拒绝的回执；不能重试')
            history = list(previous.get('preparation_history', []))
            delivery_history = list(previous.get('delivery_history', []))
            if len(delivery_history) >= 2:
                raise ValueError('已达到两次重试上限；保留失败记录')
            from datetime import datetime, timezone
            delay = 900 if previous.get('http_status') == 429 else (60 if not delivery_history else 300)
            if (datetime.now(timezone.utc)-datetime.fromisoformat(old['updated_at'])).total_seconds() < delay:
                raise ValueError('尚未到重试时间，请等待退避间隔')
            delivery_history.append(dict(status=old['status'],phase=old['phase'],updated_at=old['updated_at'],
                client_message_id=old['client_message_id'],retry_authorization=retry_note.strip(),
                evidence={k:v for k,v in previous.items() if k not in ('preparation_history','delivery_history')}))
        elif resume_note is not None:
            if not old or old['status'] != 'failed' or old['phase'] not in ('identity', 'create', 'ticket', 'prepare_send'):
                raise ValueError('只能继续已有明确证据证明尚未提交消息的失败准备')
            previous = json.loads(old['evidence'])
            delivery_history = list(previous.get('delivery_history', []))
            if previous.get('submission_reserved') is not False or job['status'] != 'failed':
                raise ValueError('缺少未提交证据；不能继续或重发')
            history = list(previous.get('preparation_history', []))
            if len(history) >= 3:
                raise ValueError('准备继续次数已达上限；停止请求')
            history.append(dict(status=old['status'], phase=old['phase'], updated_at=old['updated_at'],
                                evidence={k: v for k, v in previous.items() if k != 'preparation_history'},
                                continuation_note=resume_note.strip()))
        elif job['status'] not in RETRYABLE:
            raise ValueError('该任务已提交或处于不可重发状态')
        if issues:
            return {'status': 'not_connected', 'detail': '；'.join(issues)}
        receiver = conditions(job, person, source, settings, authorization=authorization, connection=c)
        digest = hashlib.sha256(json.dumps([settings['sender_uid'], receiver, job['content']], ensure_ascii=False).encode()).hexdigest()
        if old and (old['dedupe_key'] != digest or old['sender_uid'] != settings['sender_uid'] or old['recipient_uid'] != receiver):
            raise ValueError('继续准备不能改变账号、接收方或消息内容')
        if not old and c.execute('SELECT 1 FROM uid_message_attempts WHERE dedupe_key=?', (digest,)).fetchone():
            raise ValueError('相同账号、接收方和消息已有尝试记录；不会重复发送')
        client_id = old['client_message_id'] if old and retry_note is None else str(uuid.uuid4())
        if old:
            c.execute("UPDATE uid_message_attempts SET status='submitting',phase='prepare',detail=?,client_message_id=?,evidence=?,updated_at=? WHERE job_id=?", ('按明确授权重试平台拒绝的消息' if retry_note is not None else '明确继续未提交的准备；保留同一消息标识', client_id,json.dumps({'preparation_history': history,'delivery_history':delivery_history}), app.now(), job_id))
        else:
            c.execute("INSERT INTO uid_message_attempts(job_id,dedupe_key,sender_uid,recipient_uid,client_message_id,status,phase,detail,created_at,updated_at) VALUES(?,?,?,?,?,'submitting','prepare','已登记唯一尝试，正在核对身份与会话',?,?)", (job_id, digest, settings['sender_uid'], receiver, client_id, app.now(), app.now()))
        if authorization is not None:
            initial_evidence = {'operator_authorization': authorization}
            if history:
                initial_evidence['preparation_history'] = history
            if delivery_history:
                initial_evidence['delivery_history'] = delivery_history
            c.execute('UPDATE uid_message_attempts SET evidence=? WHERE job_id=?', (json.dumps(initial_evidence, ensure_ascii=False), job_id))
        c.execute("UPDATE message_jobs SET status='submitting',detail='HTTP 处理中；请勿重复提交',updated_at=? WHERE id=?", (app.now(), job_id))

    submission_reserved = False
    submission_checked = False
    freshness_block = None
    timing_at_submit = None

    def before_submit():
        nonlocal submission_reserved, submission_checked, freshness_block, timing_at_submit
        if submission_checked:
            raise ValueError('本次消息已保留提交次数；不会再次提交')
        submission_checked = True
        current, current_issues = config(authorized_recipient=authorized_uid)
        if current_issues or current != settings:
            raise ValueError('配置已改变，停止发送')
        with app.LOCKS['live'], app.db() as c:
            updated_job, updated_person, updated_source = snapshot(c, job_id)
            try:
                conditions(updated_job, updated_person, updated_source, current, authorization=authorization, connection=c)
                if authorization is not None:
                    kind = 'group' if 'group_id' in authorization else 'live' if 'live_id' in authorization else 'comment'
                    timing_at_submit = demand_freshness.require(c, kind, authorization[kind + '_id'])
            except demand_freshness.FreshnessError as exc:
                freshness_block = exc.evidence
                raise
            if updated_person != person or updated_job['content'] != job['content'] or updated_job['status'] != 'submitting':
                raise ValueError('发送对象、联系依据或内容已变化')
            c.execute("UPDATE uid_message_attempts SET phase='send',updated_at=? WHERE job_id=?", (app.now(), job_id))
        submission_reserved = True

    try:
        result = transport(settings, receiver, job['content'], client_id, before_submit)
        if not isinstance(result, dict) or result.get('status') not in ('accepted', 'failed', 'unknown'):
            raise ValueError()
        if result['status'] == 'accepted' and not result.get('server_message_id'):
            raise ValueError()
    except Exception:
        result = {'status': 'unknown', 'detail': '提交结果不确定；请核对会话，不会自动重发', 'phase': 'unknown'}
    if freshness_block is not None and not submission_reserved:
        result = dict(status='failed', phase='prepare_send')
    # Store only this allowlist, not credentials, raw responses, arbitrary errors.
    evidence = {key: result[key] for key in ('phase', 'http_status', 'response_sha256', 'server_message_id', 'conversation_id', 'conversation_short_id', 'platform_code', 'send_status', 'check_code', 'platform_message', 'platform_reason_code', 'transport_phase', 'transport_error', 'response_bytes') if key in result}
    if result.get('identity_reason') in ('preparation_failed', 'response_unavailable', 'http_status_rejected',
            'unexpected_content_type', 'invalid_json', 'invalid_profile', 'platform_rejected',
            'sender_mismatch', 'transport_failed'):
        evidence['identity_reason'] = result['identity_reason']
    evidence['submission_reserved'] = submission_reserved or result['status'] != 'failed' or result.get('phase') not in ('identity', 'create', 'ticket', 'prepare_send')
    if freshness_block is not None:
        evidence['demand_freshness'] = freshness_block
    if timing_at_submit is not None:
        evidence['demand_time_check'] = timing_at_submit
    if authorization is not None:
        evidence['operator_authorization'] = authorization
    if history:
        evidence['preparation_history'] = history
    if delivery_history:
        evidence['delivery_history'] = delivery_history
    detail = {'accepted': 'HTTP 服务端接受，尚无送达或已读证据', 'failed': 'HTTP 处理失败；查看阶段记录，不会自动重发', 'unknown': 'HTTP 提交结果未知；请核对会话，不会自动重发'}[result['status']]
    if result['status'] == 'failed':
        if evidence['submission_reserved'] is False:
            detail = {'identity': '账号认证未通过，消息尚未提交', 'create': '会话建立未完成，消息尚未提交',
                'ticket': '会话凭据准备失败，消息尚未提交', 'prepare_send': '发送准备未完成，消息尚未提交'}[result['phase']]
        else:
            detail = ('平台拒绝：'+evidence['platform_message']) if evidence.get('platform_message') else '发送未成功；请查看返回阶段与回执'
        if freshness_block is not None:
            detail = freshness_block['detail'] + '；消息尚未提交'
    with app.LOCKS['live'], app.db() as c:
        c.execute('UPDATE uid_message_attempts SET status=?,phase=?,detail=?,evidence=?,updated_at=? WHERE job_id=?', (result['status'], result.get('phase', 'unknown'), detail, json.dumps(evidence), app.now(), job_id))
        c.execute('UPDATE message_jobs SET status=?,detail=?,updated_at=? WHERE id=?', (result['status'], detail, app.now(), job_id))
        if result['status'] == 'accepted':
            c.execute("INSERT OR IGNORE INTO messages(lead_id,job_id,direction,content,status,created_at) VALUES(?,?,'outbound',?,'accepted',?)", (job['lead_id'], job_id, job['content'], app.now()))
        app.event(c, 'uid_message', f'HTTP 任务 #{job_id}：{detail}')
    return {'status': result['status'], 'detail': detail, 'evidence': evidence}


def recover():
    """Run once at service startup. Never starts network work."""
    with app.LOCKS['live'], app.db() as c:
        ids = [row[0] for row in c.execute("SELECT job_id FROM uid_message_attempts WHERE status='submitting'")]
        for job_id in ids:
            detail = '服务中断，提交结果未知；不自动恢复或重发'
            c.execute("UPDATE uid_message_attempts SET status='unknown',detail=?,updated_at=? WHERE job_id=?", (detail, app.now(), job_id))
            c.execute("UPDATE message_jobs SET status='unknown',detail=?,updated_at=? WHERE id=?", (detail, app.now(), job_id))
