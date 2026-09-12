"""Durable discovery provenance and bounded, fair selection of returned videos.

Only videos present in this discovery response are eligible. Saved candidates
never mask failed discovery, expand a fixed-video task, or resume a stopped plan.
"""
import hashlib
import json
import re
from datetime import datetime

VERSION = 'candidate-vertical-rotation-v2'
LEGACY_VERSION = 'candidate-rotation-v1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS collection_candidates (
 scope_key TEXT NOT NULL, video_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
 transport TEXT NOT NULL, video_title TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL, last_seen_task_id INTEGER NOT NULL REFERENCES collection_tasks(id),
 last_selected_at TEXT, last_finished_at TEXT, selections INTEGER NOT NULL DEFAULT 0,
 completed_reads INTEGER NOT NULL DEFAULT 0, quiet_streak INTEGER NOT NULL DEFAULT 0,
 new_recent_comments INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(scope_key,video_id)
);
CREATE TABLE IF NOT EXISTS collection_candidate_reads (
 task_id INTEGER NOT NULL REFERENCES collection_tasks(id), scope_key TEXT NOT NULL,
 video_id TEXT NOT NULL, settled INTEGER NOT NULL DEFAULT 0, outcome TEXT,
 new_recent_comments INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(task_id,video_id)
);
CREATE INDEX IF NOT EXISTS idx_candidate_scope_seen ON collection_candidates(scope_key,last_seen_at);
CREATE INDEX IF NOT EXISTS idx_observation_video_identity ON collection_observations(kind,page_url,external_id,task_id);
'''


def scope(task):
    return hashlib.sha256(json.dumps([task['kind'], task['target'], task['transport']], ensure_ascii=False).encode()).hexdigest()


def eligible(task):
    return task is not None and task['kind'] in ('search', 'author')


def millis(stamp):
    value = datetime.fromisoformat(stamp)
    if value.tzinfo is None:
        raise ValueError('候选时间缺少时区')
    return int(value.timestamp()*1000)


def configuration(c, task):
    if not eligible(task):
        return None
    key = scope(task)
    plan = c.execute('SELECT interval_seconds FROM collection_plans WHERE continuous=1 AND kind=? AND target=? AND transport=?',
                     (task['kind'], task['target'], task['transport'])).fetchone()
    interval = plan[0] if plan else 30
    rows = c.execute('SELECT * FROM collection_candidates WHERE scope_key=? ORDER BY julianday(last_seen_at) DESC,video_id LIMIT 500', (key,)).fetchall()
    history = []
    for row in rows:
        if not row['last_selected_at']:
            continue
        selected = millis(row['last_selected_at'])
        finished = millis(row['last_finished_at']) if row['last_finished_at'] else selected
        delay = interval * 1000 * 2**min(row['quiet_streak'], 5)
        history.append({'video_id': row['video_id'], 'last_selected_ms': selected, 'priority_ms': finished+delay})
    rounds = c.execute('SELECT COUNT(DISTINCT task_id) FROM collection_candidate_reads WHERE scope_key=?', (key,)).fetchone()[0]
    # Freeze already classified assets with this dispatch. Membership only ranks
    # candidates returned by this request; it never adds a saved work to its scope.
    vertical = [r[0] for r in c.execute("""SELECT asset_key FROM asset_verticality
        WHERE kind='work' AND json_extract(result,'$.matched')=1 ORDER BY updated_at DESC,asset_key LIMIT 10001""")]
    return {'version': VERSION, 'as_of_ms': millis(task['created_at']), 'round': rounds, 'history': history,
            'vertical_ids': vertical[:10000], 'vertical_snapshot_truncated': len(vertical)>10000}


def select(rows, limit, policy):
    """Prefer known vertical assets and retain fair slots within both pools.

    Priority times rank candidates; they are not promises of exact polling time.
    A single-video batch uses the fair slot every third round.
    """
    if not policy:
        return rows[:limit]
    if policy.get('version') not in (VERSION, LEGACY_VERSION) or type(limit) is not int or not 1 <= limit <= 5 or len(rows)>50:
        raise ValueError('候选轮换配置无效')
    history = {r['video_id']: r for r in policy['history']}
    candidates = list(enumerate(rows))
    def take(pool, budget):
        remaining = list(pool)
        chosen = []
        if budget and remaining and (budget > 1 or policy['round'] % 3 == 0):
            fair = min(remaining, key=lambda item: (history.get(item[1]['video_id'], {}).get('last_selected_ms', -1), item[0]))
            chosen.append(fair)
            remaining.remove(fair)
        remaining.sort(key=lambda item: (history.get(item[1]['video_id'], {}).get('priority_ms', policy['as_of_ms']), item[0]))
        return chosen + remaining[:budget-len(chosen)]
    known = set(policy.get('vertical_ids', [])) if policy['version'] == VERSION else set()
    vertical = [item for item in candidates if item[1]['video_id'] in known]
    ordinary = [item for item in candidates if item[1]['video_id'] not in known]
    if vertical and ordinary:
        vertical_budget = int(policy['round'] % 3 != 2) if limit == 1 else min(limit-1, (limit*2+2)//3)
        selected = take(vertical, vertical_budget) + take(ordinary, limit-vertical_budget)
        used = {item[0] for item in selected}
        selected += take([item for item in candidates if item[0] not in used], limit-len(selected))
    else:
        selected = take(candidates, limit)
    return [row for _, row in selected]


def selection_evidence(rows, selected, policy):
    known = set(policy.get('vertical_ids', [])) if policy['version'] == VERSION else set()
    return dict(priority_basis='dispatch_asset_snapshot' if policy['version']==VERSION else 'read_history_only',
                vertical_candidates=sum(r['video_id'] in known for r in rows),
                selected_vertical=sum(r['video_id'] in known for r in selected),
                vertical_snapshot_truncated=bool(policy.get('vertical_snapshot_truncated', False)))


def record(c, task, records):
    if not eligible(task) or task['finished_at']:
        raise ValueError('候选来源与任务不匹配')
    if task['status'] == 'cancelling':
        return
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        raise ValueError('每批最多保存 50 个实际返回候选')
    prepared = []
    seen = set()
    for row in records:
        vid = row.get('video_id')
        if not isinstance(vid, str) or not re.fullmatch(r'[0-9]{5,30}', vid) or vid in seen:
            raise ValueError('候选视频 ID 无效或重复')
        seen.add(vid)
        title = row.get('video_title') or vid
        if not isinstance(title, str):
            raise ValueError('候选文案无效')
        prepared.append((scope(task), vid, task['kind'], task['target'], task['transport'], title[:300],
                         task['created_at'], task['created_at'], task['id']))
    c.executemany('''INSERT INTO collection_candidates(scope_key,video_id,kind,target,transport,video_title,first_seen_at,last_seen_at,last_seen_task_id)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_key,video_id) DO UPDATE SET
        video_title=CASE WHEN excluded.video_title!=excluded.video_id THEN excluded.video_title ELSE collection_candidates.video_title END,
        last_seen_at=excluded.last_seen_at,last_seen_task_id=excluded.last_seen_task_id''', prepared)


def mark_selected(c, task, records):
    if not eligible(task) or task['finished_at'] or task['status'] == 'cancelling':
        return
    key = scope(task)
    for row in records:
        vid = row['video_id']
        if not c.execute('SELECT 1 FROM collection_candidates WHERE scope_key=? AND video_id=?', (key,vid)).fetchone():
            continue  # Legacy/fixed checkpoints never fabricate discovery evidence.
        added = c.execute('INSERT OR IGNORE INTO collection_candidate_reads(task_id,scope_key,video_id) VALUES(?,?,?)', (task['id'],key,vid)).rowcount
        if added:
            c.execute('UPDATE collection_candidates SET selections=selections+1,last_selected_at=? WHERE scope_key=? AND video_id=?',
                      (task['created_at'],key,vid))


def settle(c, task):
    if not eligible(task) or not task['finished_at']:
        return
    rows = c.execute('''SELECT r.*,p.status AS checkpoint_status FROM collection_candidate_reads r
        JOIN collection_checkpoints p ON p.task_id=r.task_id AND p.video_id=r.video_id WHERE r.task_id=? AND r.settled=0''', (task['id'],)).fetchall()
    for row in rows:
        # Unique *first observed* recent comments, including keyword-filtered
        # text. Re-reading an old comment never keeps a work falsely active.
        count = c.execute('''SELECT COUNT(*) FROM collection_observations o WHERE o.task_id=? AND o.kind='comment' AND o.page_url=?
            AND julianday(o.published_at)>=julianday(?,'-1 hour') AND julianday(o.published_at)<=julianday(o.observed_at)
            AND NOT EXISTS(SELECT 1 FROM collection_observations p WHERE p.kind='comment' AND p.page_url=o.page_url
                           AND p.external_id=o.external_id AND p.task_id<o.task_id)''',
            (task['id'],'https://www.douyin.com/video/'+row['video_id'],task['created_at'])).fetchone()[0]
        done = row['checkpoint_status'] == 'done'
        c.execute('''UPDATE collection_candidates SET completed_reads=completed_reads+?,new_recent_comments=new_recent_comments+?,
            quiet_streak=CASE WHEN ?>0 THEN 0 WHEN ? THEN MIN(quiet_streak+1,5) ELSE quiet_streak END,
            last_finished_at=CASE WHEN ? THEN ? ELSE last_finished_at END WHERE scope_key=? AND video_id=?''',
            (int(done),count,count,int(done),int(done),task['finished_at'],row['scope_key'],row['video_id']))
        c.execute('UPDATE collection_candidate_reads SET settled=1,outcome=?,new_recent_comments=? WHERE task_id=? AND video_id=?',
                  (row['checkpoint_status'],count,task['id'],row['video_id']))


def state(c):
    return {'policy': VERSION, 'scope_count': c.execute('SELECT COUNT(DISTINCT scope_key) FROM collection_candidates').fetchone()[0],
            'candidate_count': c.execute('SELECT COUNT(*) FROM collection_candidates').fetchone()[0],
            'unique_videos': c.execute('SELECT COUNT(DISTINCT video_id) FROM collection_candidates').fetchone()[0],
            'selected_videos': c.execute('SELECT COUNT(*) FROM collection_candidates WHERE selections>0').fetchone()[0],
            'completed_reads': c.execute('SELECT COALESCE(SUM(completed_reads),0) FROM collection_candidates').fetchone()[0],
            'new_recent_comments': c.execute('SELECT COALESCE(SUM(new_recent_comments),0) FROM collection_candidates').fetchone()[0]}
