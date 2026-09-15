"""Versioned analysis evidence. Source text and human judgments are not rewritten."""
import hashlib
import json

import clubops as app

SCHEMA = '''
CREATE TABLE IF NOT EXISTS intent_results (
 id INTEGER PRIMARY KEY, evidence_type TEXT NOT NULL, record_id INTEGER NOT NULL,
 method TEXT NOT NULL, engine TEXT NOT NULL, request_id TEXT NOT NULL,
 input_hash TEXT NOT NULL, input_json TEXT NOT NULL, status TEXT NOT NULL,
 result_json TEXT NOT NULL DEFAULT '{}', detail TEXT NOT NULL DEFAULT '',
 started_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(evidence_type,record_id,method,request_id)
);
CREATE INDEX IF NOT EXISTS idx_intent_record ON intent_results(evidence_type,record_id,method,id);
'''


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inputs(c, kind, record_id):
    if kind not in ('comment', 'live', 'group') or type(record_id) is not int or not 0 < record_id < 2**63:
        raise ValueError('请选择有效的评论或弹幕记录')
    if kind == 'comment':
        row = c.execute('''SELECT x.*,v.title,(SELECT p.raw_text FROM comments p
          WHERE p.source_id=x.source_id AND p.video_id=x.video_id AND p.id!=x.id
          AND p.external_id=x.parent_external_id) AS parent_text
          FROM comments x JOIN videos v ON v.id=x.video_id WHERE x.id=?''', (record_id,)).fetchone()
    elif kind == 'group':
        row = c.execute('SELECT m.*,m.group_title AS title FROM group_messages m WHERE m.id=?', (record_id,)).fetchone()
    else:
        row = c.execute('''SELECT m.*,COALESCE(r.title,'') AS title FROM live_messages m
          JOIN live_sessions s ON s.id=m.session_id LEFT JOIN live_rooms r ON r.room_url=s.room_url
          WHERE m.id=?''', (record_id,)).fetchone()
    if not row:
        raise ValueError('原文记录不存在')
    source=dict(kind=kind, text=row['raw_text'], parent=(row['parent_text'] or '') if kind == 'comment' else '',title=row['title'])
    import author_roles
    author=author_roles.context(c,kind,record_id)
    if author:source['author']=author
    return source,row


def capture_rule(c, kind, record_id):
    source, row = inputs(c, kind, record_id)
    if row['analysis_method'] != 'rules':
        return
    result = {key: row[key] for key in ('category', 'game', 'reason', 'analysis_method')}
    result['confidence'] = row['confidence'] if 'confidence' in row.keys() else None
    result['facts'] = json.loads(row['facts'])
    engine = result['facts'].get('rules_version', 'legacy-rules')
    # Repeat initialization doesn't create duplicate snapshots or rerun rules.
    key = digest([digest(source), engine, result])
    c.execute('''INSERT OR IGNORE INTO intent_results(evidence_type,record_id,method,engine,request_id,
       input_hash,input_json,status,result_json,started_at,finished_at)
       VALUES(?,?,'rules',?,?,?,?, 'completed',?,?,?)''',
       (kind, record_id, engine, key, digest(source), json.dumps(source, ensure_ascii=False),
        json.dumps(result, ensure_ascii=False), app.now(), app.now()))


def migrate(c):
    c.executescript(SCHEMA)
    for kind, table in (('comment', 'comments'), ('live', 'live_messages'), ('group', 'group_messages')):
        for row in c.execute(f"""SELECT x.id FROM {table} x WHERE analysis_method='rules'
            AND NOT EXISTS(SELECT 1 FROM intent_results r WHERE r.evidence_type=?
            AND r.record_id=x.id AND r.method='rules')""", (kind,)).fetchall():
            capture_rule(c, kind, row['id'])
    # Legacy human rows don't retain the original rule category/reason. Do not
    # reconstruct them by guessing or by running a newer classifier over history.


def latest(c, kind, record_id, method, input_hash):
    row = c.execute('''SELECT * FROM intent_results WHERE evidence_type=? AND record_id=?
        AND method=? AND input_hash=? ORDER BY id DESC LIMIT 1''', (kind, record_id, method, input_hash)).fetchone()
    if not row:
        return None
    result = {key: row[key] for key in ('id', 'method', 'engine', 'status', 'input_hash', 'detail', 'started_at', 'finished_at')}
    result['result'] = json.loads(row['result_json'])
    return result


def compatible_engine(recorded, current):
    """Recall extensions retain earlier decisions for identical source inputs.
    v10 applies the stricter ordinary-teamup guard to every projected result,
    including compatible older model results, before display or outreach.

    Other model, provider, schema or prompt changes still require a new result.
    This never rewrites historical records or schedules another model request.
    """
    if not current or not recorded:
        return False
    if recorded == current:
        return True
    return any(f':intent-prompt-v{old}:' in recorded and
               recorded.replace(f':intent-prompt-v{old}:', f':intent-prompt-v{new}:') == current
               for old, new in ((6, 7), (6, 8), (7, 8), (6, 9), (7, 9), (8, 9), (6, 10), (7, 10), (8, 10), (9, 10),
                                (6,11),(7,11),(8,11),(9,11),(10,11)))


def project(c, row, *, model_engine=None, details=True):
    kind = row.get('evidence_type', 'comment')
    source = dict(kind=kind, text=row['raw_text'], parent='', title='')
    if kind == 'comment':
        source['title'] = row['video_title']
        parent = row.get('parent_context', {})
        if parent.get('status') == 'available':
            source['parent'] = parent['raw_text']
    elif kind=='group':source['title']=row['group_title']
    else:source['title']=row.get('room_title','')
    import author_roles
    author=author_roles.context(c,kind,row['id'])
    if author:source['author']=author
    fingerprint = digest(source)
    row['analysis_input_hash'] = fingerprint
    rule = latest(c, kind, row['id'], 'rules', fingerprint) if details else None
    if rule:
        row['rule_result'] = rule
        row['rule_category'], row['rule_reason'] = rule['result']['category'], rule['result']['reason']
    elif row['analysis_method'] == 'rules':
        row['rule_category'], row['rule_reason'] = row['category'], row['reason']
    else:
        row.setdefault('rule_category', None)
        row.setdefault('rule_reason', '旧记录未独立保存规则分类与理由')
    model = latest(c, kind, row['id'], 'model', fingerprint)
    row['model_result'] = model
    if model and model['status'] == 'completed' and compatible_engine(model['engine'], model_engine) and row['analysis_method'] not in ('human', 'pending'):
        row.update(model['result'])
    import service_roles
    service_roles.project(row,source)
    author_roles.project(c,row,source)
    row['author_profile']=source.get('author')
    if not details:return row
    if kind == 'comment':
        import asset_keywords
        relevance=asset_keywords.message_relevance(c,source['text'],source['title'],source['parent'])
        row['companion_relevance']=relevance
    import asset_verticality
    row['model_routing']=asset_verticality.routing(c,kind,row['id'])
    return row


def history(kind, record_id, mode='live'):
    with app.db(mode) as c:
        source, _ = inputs(c, kind, record_id)
        rows = c.execute('''SELECT id,method,engine,input_hash,status,result_json,detail,started_at,finished_at
            FROM intent_results WHERE evidence_type=? AND record_id=? ORDER BY id DESC LIMIT 30''', (kind, record_id)).fetchall()
        return dict(input_hash=digest(source), rows=[dict(r, result=json.loads(r['result_json'])) for r in rows])


def recover():
    with app.LOCKS['live'], app.db() as c:
        c.execute("UPDATE intent_results SET status='interrupted',detail='服务中断，保留规则结果；没有自动重跑',finished_at=? WHERE method='model' AND status='running'", (app.now(),))
