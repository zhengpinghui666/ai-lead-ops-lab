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


def config():
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
    value, issues = config()
    try:
        sender = uid_protocol.numeric_uid(value.get('sender_uid'))
    except ValueError:
        sender = ''
    return dict(status='not_configured' if issues else 'configured_unverified', transport='http',
                can_attempt=not issues, sender_uid=sender,
                allowed_recipients=len(value.get('allowed_recipient_uids', [])) if isinstance(value.get('allowed_recipient_uids'), list) else 0,
                live_verified=False, issues=issues,
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


def conditions(job, person, source, settings):
    if person['do_not_contact'] or person['contact_basis'] not in ('inbound', 'opt_in') or not person['contact_note'].strip():
        raise ValueError('缺少联系依据或已禁止联系；未发送')
    uid = uid_protocol.numeric_uid(person['external_id'])
    if source['kind'] not in ('browser', 'uid_test'):
        raise ValueError('来源未明确记录数字 UID；不能将导入的 OpenID 或抖音号用于发送')
    if uid not in settings.get('allowed_recipient_uids', []):
        raise ValueError('接收方不在本次已同意测试范围内；未发送')
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


def send_one(job_id, mode='live', *, transport=None, resume_note=None):
    """Resume is an explicit local maintenance action, never an automatic retry.

    Only a proven pre-submission failure can continue, with the same immutable
    sender, recipient, content, dedupe key and client ID. HTTP routes do not
    expose resume_note. Legacy records without submission evidence stay closed.
    """
    if mode != 'live':
        raise ValueError('演示区不允许个人号 HTTP 发送')
    if not GUARD.acquire(blocking=False):
        raise ValueError('已有一条 HTTP 任务正在处理；请等待完成')
    try:
        return _send_one(job_id, transport or uid_transport.send, resume_note)
    finally:
        GUARD.release()


def _send_one(job_id, transport, resume_note=None):
    if resume_note is not None and (not isinstance(resume_note, str) or not resume_note.strip() or len(resume_note) > 500):
        raise ValueError('继续准备需要明确的本地处理依据')
    settings, issues = config()
    history = []
    with app.LOCKS['live'], app.db() as c:
        c.execute('BEGIN IMMEDIATE')
        job, person, source = snapshot(c, job_id)
        old = c.execute('SELECT * FROM uid_message_attempts WHERE job_id=?', (job_id,)).fetchone()
        if old and resume_note is None:
            if old['status'] == 'submitting':
                # GUARD was acquired by this call, so no local attempt is active.
                # A lost final DB write is uncertainty, never permission to retry.
                return {'status': 'unknown', 'detail': '先前尝试没有完整结果；请核对会话，不会重发'}
            return {key: old[key] for key in ('status', 'detail')}
        if resume_note is not None:
            if not old or old['status'] != 'failed' or old['phase'] not in ('identity', 'create', 'ticket', 'prepare_send'):
                raise ValueError('只能继续已有明确证据证明尚未提交消息的失败准备')
            previous = json.loads(old['evidence'])
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
        receiver = conditions(job, person, source, settings)
        digest = hashlib.sha256(json.dumps([settings['sender_uid'], receiver, job['content']], ensure_ascii=False).encode()).hexdigest()
        if old and (old['dedupe_key'] != digest or old['sender_uid'] != settings['sender_uid'] or old['recipient_uid'] != receiver):
            raise ValueError('继续准备不能改变账号、接收方或消息内容')
        if not old and c.execute('SELECT 1 FROM uid_message_attempts WHERE dedupe_key=?', (digest,)).fetchone():
            raise ValueError('相同账号、接收方和消息已有尝试记录；不会重复发送')
        client_id = old['client_message_id'] if old else str(uuid.uuid4())
        if old:
            c.execute("UPDATE uid_message_attempts SET status='submitting',phase='prepare',detail='明确继续未提交的准备；保留同一消息标识',evidence=?,updated_at=? WHERE job_id=?", (json.dumps({'preparation_history': history}), app.now(), job_id))
        else:
            c.execute("INSERT INTO uid_message_attempts(job_id,dedupe_key,sender_uid,recipient_uid,client_message_id,status,phase,detail,created_at,updated_at) VALUES(?,?,?,?,?,'submitting','prepare','已登记唯一尝试，正在核对身份与会话',?,?)", (job_id, digest, settings['sender_uid'], receiver, client_id, app.now(), app.now()))
        c.execute("UPDATE message_jobs SET status='submitting',detail='HTTP 处理中；请勿重复提交',updated_at=? WHERE id=?", (app.now(), job_id))

    submission_reserved = False

    def before_submit():
        nonlocal submission_reserved
        if submission_reserved:
            raise ValueError('本次消息已保留提交次数；不会再次提交')
        submission_reserved = True
        current, current_issues = config()
        if current_issues or current != settings:
            raise ValueError('配置已改变，停止发送')
        with app.LOCKS['live'], app.db() as c:
            updated_job, updated_person, updated_source = snapshot(c, job_id)
            conditions(updated_job, updated_person, updated_source, current)
            if updated_person != person or updated_job['content'] != job['content'] or updated_job['status'] != 'submitting':
                raise ValueError('发送对象、联系依据或内容已变化')
            c.execute("UPDATE uid_message_attempts SET phase='send',updated_at=? WHERE job_id=?", (app.now(), job_id))

    try:
        result = transport(settings, receiver, job['content'], client_id, before_submit)
        if not isinstance(result, dict) or result.get('status') not in ('accepted', 'failed', 'unknown'):
            raise ValueError()
        if result['status'] == 'accepted' and not result.get('server_message_id'):
            raise ValueError()
    except Exception:
        result = {'status': 'unknown', 'detail': '提交结果不确定；请核对会话，不会自动重发', 'phase': 'unknown'}
    # Store only this allowlist, not credentials, raw responses, arbitrary errors.
    evidence = {key: result[key] for key in ('phase', 'http_status', 'response_sha256', 'server_message_id', 'conversation_id', 'conversation_short_id', 'platform_code', 'transport_phase', 'transport_error', 'response_bytes') if key in result}
    evidence['submission_reserved'] = submission_reserved or result['status'] != 'failed' or result.get('phase') not in ('identity', 'create', 'ticket', 'prepare_send')
    if history:
        evidence['preparation_history'] = history
    detail = {'accepted': 'HTTP 服务端接受，尚无送达或已读证据', 'failed': 'HTTP 处理失败；查看阶段记录，不会自动重发', 'unknown': 'HTTP 提交结果未知；请核对会话，不会自动重发'}[result['status']]
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
