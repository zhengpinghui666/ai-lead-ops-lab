"""User-started verification handoff, tied to one immutable collection batch.

This module never opens a browser by itself and never supplies CAPTCHA answers.
Only a completed reader batch can reconnect an unchanged, previously active plan.
"""
import json
import clubops as app

SCHEMA = '''CREATE TABLE IF NOT EXISTS collection_manual_recoveries (
 task_id INTEGER PRIMARY KEY REFERENCES collection_tasks(id),
 parent_task_id INTEGER NOT NULL REFERENCES collection_tasks(id),
 plan_context TEXT, state TEXT NOT NULL DEFAULT 'pending',
 detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, settled_at TEXT);
CREATE TABLE IF NOT EXISTS collection_verification_retries (
 task_id INTEGER PRIMARY KEY REFERENCES collection_tasks(id),
 parent_task_id INTEGER NOT NULL UNIQUE REFERENCES collection_tasks(id),
 root_task_id INTEGER NOT NULL REFERENCES collection_tasks(id),
 retry_number INTEGER NOT NULL CHECK(retry_number BETWEEN 1 AND 2));
'''


def retry_state(c, task_id):
    row = c.execute('SELECT * FROM collection_verification_retries WHERE task_id=?', (task_id,)).fetchone()
    return dict(root_task_id=row['root_task_id'] if row else task_id,
                retry_number=row['retry_number'] if row else 0, max_retries=2,
                remaining=2-(row['retry_number'] if row else 0))


def attach_retry(c, parent_id, child_id):
    previous = retry_state(c, parent_id)
    if previous['remaining'] <= 0:
        raise ValueError('验证码额外重采已用完两次，需要人工处理')
    if c.execute('SELECT 1 FROM collection_verification_retries WHERE parent_task_id=?',(parent_id,)).fetchone():
        raise ValueError('该批次已有额外重采记录，请查看已有结果')
    c.execute('INSERT INTO collection_verification_retries VALUES(?,?,?,?)',
              (child_id, parent_id, previous['root_task_id'], previous['retry_number']+1))


def record(c, task_id):
    row = c.execute('SELECT * FROM collection_manual_recoveries WHERE task_id=?', (task_id,)).fetchone()
    return dict(row) if row else None


def eligible(c, task):
    import collection_accounts
    return bool(task and task['finished_at'] and task['status'] == 'needs_verification'
                and task['transport'] == 'local_browser'
                and collection_accounts.binding(c, task['id']))


def request(body, mode='live'):
    import collector
    if (mode != 'live' or set(body) != {'id', 'request_id'}
            or type(body['id']) is not int or body['id'] < 1):
        raise ValueError('人工验证只接受原批次 ID 和本次请求 ID')
    return collector.start({'kind': 'search', 'target': '人工处理原批次验证',
                            'request_id': body['request_id']}, mode, manual_from=body['id'])


def attach(c, parent_id, child_id):
    rows = c.execute('SELECT * FROM collection_plans WHERE last_task_id=?', (parent_id,)).fetchall()
    if len(rows) > 1:
        raise ValueError('原批次对应多个计划，请先核对计划')
    context = None
    if rows:
        p = rows[0]
        if p['status'] in ('attention', 'running') and p['activated_at']:
            context = {key: p[key] for key in ('id', 'intent_version', 'activated_at')}
    c.execute('INSERT INTO collection_manual_recoveries(task_id,parent_task_id,plan_context,created_at) VALUES(?,?,?,?)',
              (child_id, parent_id, json.dumps(context) if context else None, app.now()))
    # Keep the fault visible while the user is solving the challenge.
    if context:
        c.execute("UPDATE collection_plans SET status='attention',next_run_at=NULL,detail=?,updated_at=? WHERE id=?",
                  (f'原批次 #{parent_id} 正在人工处理验证；等待 #{child_id} 实际读取完成后恢复', app.now(), context['id']))


def settle(c, task_id):
    import collection_accounts
    item = record(c, task_id)
    if not item or item['settled_at']:
        return
    task = c.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
    if not task or not task['finished_at']:
        return
    state, detail = 'needs_user', '本次尚未恢复有效读取，监控保持待处理'
    context = json.loads(item['plan_context']) if item['plan_context'] else None
    parent = collection_accounts.binding(c, item['parent_task_id'])
    child = collection_accounts.binding(c, task_id)
    same_account = bool(parent and child and all(parent[k] == child[k] for k in ('account_id','sender_uid','storage','role')))
    if task['status'] == 'completed' and same_account:
        state, detail = 'completed', '本批已完成有效读取；未关联自动监控计划'
        if context:
            p = c.execute('SELECT * FROM collection_plans WHERE id=?', (context['id'],)).fetchone()
            unchanged = bool(p and p['last_task_id'] == item['parent_task_id']
                and p['intent_version'] == context['intent_version']
                and p['activated_at'] == context['activated_at']
                and p['status'] in ('attention', 'running'))
            if unchanged:
                settled = int(p['settled_task_id'] == item['parent_task_id'])
                if p['run_count'] < 1 or p['settled_count'] < settled:
                    state, detail = 'needs_user', '本批读取完成，但原计划计数异常，请检查计划'
                else:
                    c.execute("""UPDATE collection_plans SET last_task_id=?,settled_count=settled_count-?,
                        status='running',next_run_at=NULL,detail=?,updated_at=? WHERE id=?""",
                        (task_id, settled, '人工处理后已恢复读取，按原计划继续调度', app.now(), p['id']))
                    state, detail = 'recovered', '本批已完成有效读取，已接回原监控计划'
            else:
                state, detail = 'plan_changed', '本批已完成读取；原计划已关闭、修改或重启，保留你的最新设置'
    elif task['status'] in ('cancelled', 'interrupted', 'timeout', 'session_expired'):
        state, detail = 'stopped', '人工处理已停止或超时，监控未恢复'
    c.execute('UPDATE collection_manual_recoveries SET state=?,detail=?,settled_at=? WHERE task_id=?',
              (state, detail, app.now(), task_id))
    app.event(c, 'verification-recovery', f'原批次 #{item["parent_task_id"]} → #{task_id}：{detail}')


def public(c, task_id):
    item = record(c, task_id)
    if not item:
        return None
    return {k: item[k] for k in ('parent_task_id', 'state', 'detail', 'created_at', 'settled_at')} | {
        'resume_monitor': bool(item['plan_context'])}
