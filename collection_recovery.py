"""User-started verification handoff, tied to one immutable collection batch.

This module never opens a browser by itself and never supplies CAPTCHA answers.
Only verified reader results can reconnect an unchanged, previously active plan.
"""
import json
from datetime import datetime, timedelta
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


def manual_search_body_restored(c, task):
    """An old manual search prompt must not invalidate later verified work reads.

    This is not a retry classification or a CAPTCHA pass verdict. A terminal
    partial batch has passed the reader's final access guard. Only its initial
    empty search prompt may precede strictly validated same-work body loss.
    """
    if (task['kind'] != 'search' or task['transport'] != 'local_browser'
            or task['interactive'] != 1 or task['status'] != 'partial'
            or not task['finished_at'] or not record(c, task['id'])):
        return False
    from urllib.parse import urlsplit, unquote
    import re
    import collection_scheduler
    phases, attempt, prompts, following = [], None, 0, []
    try:
        for row in c.execute('SELECT stage,snapshot FROM collection_diagnostics WHERE task_id=? ORDER BY id', (task['id'],)):
            stage, s = row['stage'], json.loads(row['snapshot'])
            if stage not in ('captcha_dom','needs_verification','captcha_workflow'):
                following.append(row)
                continue
            # A challenge after collection starts is a new gate, not old history.
            if following:
                return False
            if stage == 'captcha_workflow':
                v = s.get('verification', {})
                if (v.get('transport') != 'local_browser' or v.get('submissions') != 0
                        or not re.fullmatch(r'[a-fA-F0-9-]{36}', v.get('attempt_id', ''))):
                    return False
                if attempt is not None and attempt != v['attempt_id']:
                    return False
                attempt = v['attempt_id']
                phases.append(v.get('phase'))
                if v.get('phase') == 'needs_review' and v.get('reason') != 'manual_mode':
                    return False
            else:
                if s.get('responses') != []:
                    return False
                if stage == 'needs_verification':
                    u = urlsplit(s.get('page_url', ''))
                    if (u.scheme != 'https' or u.netloc != 'www.douyin.com'
                            or unquote(u.path) != '/search/' + task['target'] or u.query or u.fragment
                            or s.get('navigation_http_status') != 200 or s.get('navigation_error')):
                        return False
                    prompts += 1
        if phases != ['detected','needs_review'] or not 1 <= prompts <= 2:
            return False
        return collection_scheduler.transient_browser_body_wait(
            c, task, resource_missing_only=True, diagnostics=following) == 0
    except (ValueError, TypeError, AttributeError):
        return False


def reading_restored(c, task):
    """Partial coverage is distinct from a login, CAPTCHA or parsing failure."""
    if task['status'] == 'completed':
        return True
    if task['status'] != 'partial' or not task['finished_at']:
        return False
    import collection_scheduler
    if collection_scheduler.transient_batch_wait(c, task) != 0 and not manual_search_body_restored(c, task):
        return False
    try:
        expected = {r['video_url'] for r in c.execute("SELECT video_url FROM collection_checkpoints WHERE task_id=? AND status!='unavailable'", (task['id'],))}
        actual = set()
        for row in c.execute("SELECT snapshot FROM collection_diagnostics WHERE task_id=? AND stage='comment-read'", (task['id'],)):
            s = json.loads(row['snapshot'])
            if (s.get('processing', {}).get('recognized') is not True or not s.get('responses')
                or not any(r.get('kind') == 'comment' and r.get('status') == 200 and r.get('status_code') == 0 and r.get('comments_type') == 'array' for r in s['responses'])
                or any(r.get('status_code') not in (None, 0) for r in s['responses'])):
                return False
            actual.add(s['page_url'])
        return bool(expected and actual == expected)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def explicit_start_ready(c, plan, task):
    """A new explicit start may use an old manual read, even after a restart.

    This does not restore an old callback's intent: command('start') is itself
    the new intent. Without that action, restart/stop still invalidate callbacks.
    """
    import collection_accounts
    item = record(c, task['id']) if task else None
    if not item or item['parent_task_id'] != plan['last_task_id'] or not reading_restored(c, task):
        return False
    try:
        context = json.loads(item['plan_context'])
        parent = c.execute('SELECT * FROM collection_tasks WHERE id=?', (item['parent_task_id'],)).fetchone()
        before = collection_accounts.binding(c, item['parent_task_id'])
        after = collection_accounts.binding(c, task['id'])
        return bool(context and context['id'] == plan['id'] and before and after
            and all(before[k] == after[k] for k in ('account_id','sender_uid','storage','role'))
            and all(parent[k] == task[k] for k in ('kind','target','transport','lookback_hours','comment_since','include_keywords','exclude_keywords','video_limit','comment_limit','page_concurrency')))
    except (ValueError, TypeError, KeyError):
        return False


def settle(c, task_id):
    import collection_accounts
    item = record(c, task_id)
    if not item or item['settled_at']:
        return
    task = c.execute('SELECT * FROM collection_tasks WHERE id=?', (task_id,)).fetchone()
    if not task or not task['finished_at']:
        return
    state, detail = 'needs_user', ('本批已取得部分评论，但恢复条件尚未核验，监控保持待处理' if task['status'] == 'partial' else '本次尚未恢复有效读取，监控保持待处理')
    context = json.loads(item['plan_context']) if item['plan_context'] else None
    parent = collection_accounts.binding(c, item['parent_task_id'])
    child = collection_accounts.binding(c, task_id)
    same_account = bool(parent and child and all(parent[k] == child[k] for k in ('account_id','sender_uid','storage','role')))
    if reading_restored(c, task) and same_account:
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
                    if task['status'] == 'partial':
                        due = (datetime.fromisoformat(task['finished_at']) + timedelta(seconds=max(60, p['interval_seconds']))).isoformat(timespec='seconds')
                        detail = '已恢复有效读取；部分评论未完全加载，保留断点，按原监控计划继续'
                        c.execute('UPDATE collection_plans SET settled_task_id=?,settled_count=settled_count+1,next_run_at=?,detail=? WHERE id=?',
                                  (task_id, due, detail, p['id']))
            else:
                state, detail = 'plan_changed', '本批已完成读取；原计划已关闭、修改或重启，保留你的最新设置'
    elif task['status'] in ('cancelled', 'interrupted', 'timeout', 'session_expired'):
        state, detail = 'stopped', '人工处理已停止或超时，监控未恢复'
    if context and state in ('needs_user', 'stopped'):
        c.execute("UPDATE collection_plans SET detail=?,updated_at=? WHERE id=? AND last_task_id=? AND status='attention' AND intent_version=? AND activated_at=?",
                  (detail + f'；请查看批次 #{task_id}', app.now(), context['id'], item['parent_task_id'], context['intent_version'], context['activated_at']))
    c.execute('UPDATE collection_manual_recoveries SET state=?,detail=?,settled_at=? WHERE task_id=?',
              (state, detail, app.now(), task_id))
    app.event(c, 'verification-recovery', f'原批次 #{item["parent_task_id"]} → #{task_id}：{detail}')


def public(c, task_id):
    item = record(c, task_id)
    if not item:
        return None
    return {k: item[k] for k in ('parent_task_id', 'state', 'detail', 'created_at', 'settled_at')} | {
        'resume_monitor': bool(item['plan_context'])}
