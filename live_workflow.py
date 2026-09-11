"""Local live-message review and typed-UID linkage. No network operations."""
import hashlib
import json

import clubops as app
import uid_protocol

SCHEMA = '''
CREATE TABLE IF NOT EXISTS live_links (
 message_id INTEGER PRIMARY KEY REFERENCES live_messages(id),
 person_id INTEGER NOT NULL REFERENCES people(id)
);
CREATE INDEX IF NOT EXISTS idx_live_links_person ON live_links(person_id,message_id);
CREATE TABLE IF NOT EXISTS live_judgments (
 message_id INTEGER PRIMARY KEY REFERENCES live_messages(id), category TEXT NOT NULL,
 manual_fields TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL,
 version INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_reviews (
 id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES live_messages(id),
 snapshot TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_reviews_message ON live_reviews(message_id,id);
'''


def positive_id(value):
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or not 0 < int(value) < 2**63:
        raise ValueError('记录 ID 无效')
    return int(value)


def attach(c, message_id, *, allow_filtered=False):
    row = c.execute('SELECT * FROM live_messages WHERE id=?', (message_id,)).fetchone()
    if not row or not row['uid'] or row['filter_reason'] and not allow_filtered:
        return None
    uid = uid_protocol.numeric_uid(row['uid'])
    old = c.execute('SELECT person_id FROM live_links WHERE message_id=?', (message_id,)).fetchone()
    if old:
        return old['person_id']
    # The browser comment parser uses only numeric UID, never sec_uid or OpenID.
    # Reuse that namespace, including existing refusal/contact records.
    source = c.execute("SELECT id FROM sources WHERE kind='browser' ORDER BY id LIMIT 1").fetchone()
    source_id = source['id'] if source else c.execute("INSERT INTO sources(name,kind,status,notes) VALUES('抖音 · 本机浏览器','browser','unverified','数字 UID 来自实际浏览器观察；视频评论与直播原文分别保存')").lastrowid
    c.execute('INSERT OR IGNORE INTO people(source_id,external_id,nickname) VALUES(?,?,?)', (source_id, uid, row['nickname'] or '未提供昵称'))
    person_id = c.execute('SELECT id FROM people WHERE source_id=? AND external_id=?', (source_id, uid)).fetchone()[0]
    c.execute('INSERT OR IGNORE INTO leads(person_id,updated_at) VALUES(?,?)', (person_id, row['observed_at']))
    c.execute('INSERT INTO live_links(message_id,person_id) VALUES(?,?)', (message_id, person_id))
    return person_id


def migrate(c):
    c.executescript(SCHEMA)
    if 'game' not in {r[1] for r in c.execute('PRAGMA table_info(live_messages)')}:
        c.execute("ALTER TABLE live_messages ADD COLUMN game TEXT NOT NULL DEFAULT ''")
    # Existing raw records remain intact. The old adapter did not store game,
    # so leave it unknown rather than rerunning today's rules over history.
    for row in c.execute("SELECT m.id FROM live_messages m LEFT JOIN live_links k ON k.message_id=m.id WHERE k.message_id IS NULL AND m.uid IS NOT NULL AND m.filter_reason='' ORDER BY m.id").fetchall():
        attach(c, row['id'])


SELECT = '''SELECT m.*,s.room_url,k.person_id,l.id AS lead_id,
 j.category AS manual_category,j.manual_fields,j.reason AS manual_reason,
 j.version AS review_version,j.updated_at AS reviewed_at
 FROM live_messages m JOIN live_sessions s ON s.id=m.session_id
 LEFT JOIN live_links k ON k.message_id=m.id LEFT JOIN leads l ON l.person_id=k.person_id
 LEFT JOIN live_judgments j ON j.message_id=m.id'''


def token(row):
    payload = {key: row[key] for key in ('id', 'payload_hash', 'category', 'game', 'facts', 'analysis_method', 'review_version')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def project(c, row, *, history=True):
    result = dict(row)
    result['review_token'] = token(row)
    result['rule_facts'] = json.loads(row['facts'])
    result['rule_game'], result['rule_category'], result['rule_reason'] = row['game'], row['category'], row['reason']
    result['manual_fields'] = json.loads(row['manual_fields'] or '{}')
    result['facts'] = {**result['rule_facts'], **{k: v for k, v in result['manual_fields'].items() if k != 'game'}}
    result['game'] = result['manual_fields'].get('game', row['game'])
    if row['manual_category'] is not None:
        result.update(category=row['manual_category'], analysis_method='human', reason=row['manual_reason'])
    for key in ('include_matches', 'exclude_matches'):
        result[key] = json.loads(row[key])
    result.update(evidence_type='live', external_id=row['message_id'], discovered_at=row['observed_at'],
                  source_url=row['room_url'], source_title='直播间 ' + row['room_id'], source_name='抖音直播 · 浏览器观察',
                  parent_context={'status': 'none'}, confidence=None)
    result['review_history'] = [dict(id=r['id'], created_at=r['created_at'], **json.loads(r['snapshot'])) for r in
        c.execute('SELECT * FROM live_reviews WHERE message_id=? ORDER BY id DESC LIMIT 10', (row['id'],))] if history else []
    import analysis_store
    import semantic
    # Determine workspace from this connection, never apply real configuration to demo.
    filename = c.execute('PRAGMA database_list').fetchone()[2]
    model_engine = semantic.state()['engine'] if filename.endswith('clubops-live.db') else None
    analysis_store.project(c, result, model_engine=model_engine)
    return result


def records(c, where='', args=(), *, limit=500, history=True):
    return [project(c, r, history=history) for r in c.execute(SELECT + ' ' + where + ' ORDER BY m.id DESC LIMIT ?', (*args, limit)).fetchall()]


def detail(message_id, mode='live'):
    with app.db(mode) as c:
        rows = records(c, 'WHERE m.id=?', (positive_id(message_id),), limit=1)
        if not rows:
            raise ValueError('弹幕记录不存在')
        return rows[0]


def latest_by_person(c):
    # One representative per person; full live history is fetched in bounded pages.
    rows = c.execute(SELECT + ''' WHERE m.id IN (
      SELECT id FROM (SELECT x.id,ROW_NUMBER() OVER(PARTITION BY k.person_id
        ORDER BY COALESCE(julianday(x.published_at),julianday(x.observed_at)) DESC,x.id DESC) AS position
        FROM live_messages x JOIN live_links k ON k.message_id=x.id) WHERE position=1)
      ORDER BY m.id DESC''').fetchall()
    counts = {r[0]: r[1] for r in c.execute('SELECT person_id,COUNT(*) FROM live_links GROUP BY person_id')}
    return [project(c, r) for r in rows], counts


def history(lead_id, offset=0, mode='live'):
    lead_id = positive_id(lead_id)
    if isinstance(offset, bool) or not str(offset).isascii() or not str(offset).isdigit() or not 0 <= int(offset) <= 10000000:
        raise ValueError('历史分页参数无效')
    offset, limit = int(offset), 50
    with app.db(mode) as c:
        lead = app.required(c, 'leads', lead_id)
        total = c.execute('SELECT COUNT(*) FROM live_links WHERE person_id=?', (lead['person_id'],)).fetchone()[0]
        rows = c.execute(SELECT + ' WHERE k.person_id=? ORDER BY m.id DESC LIMIT ? OFFSET ?', (lead['person_id'], limit, offset)).fetchall()
        return dict(rows=[project(c, r) for r in rows], total=total, offset=offset, limit=limit)


def review(body, mode='live'):
    message_id = positive_id(body.get('id'))
    category, reason = body.get('category'), app.clean(body.get('reason'))
    if category not in app.LABELS or not reason:
        raise ValueError('请选择分类并填写原文对应的人工判断依据')
    with app.LOCKS[mode], app.db(mode) as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute(SELECT + ' WHERE m.id=?', (message_id,)).fetchone()
        if not row:
            raise ValueError('弹幕记录不存在')
        if not body.get('review_token') or body['review_token'] != token(row):
            raise ValueError('这条弹幕或判断已更新，请重新打开核对表单；本次未覆盖')
        previous = project(c, row, history=False)
        fields = app.validate_review_fields(body['manual_fields']) if 'manual_fields' in body else previous['manual_fields']
        snapshot = dict(raw_text=row['raw_text'], category=category, reason=reason, manual_fields=fields,
                        previous_fields=previous['manual_fields'], previous_category=previous['category'],
                        rule_game=row['game'], rule_category=row['category'], rule_facts=previous['rule_facts'])
        version, stamp = (row['review_version'] or 0) + 1, app.now()
        c.execute('INSERT INTO live_reviews(message_id,snapshot,created_at) VALUES(?,?,?)', (message_id, json.dumps(snapshot, ensure_ascii=False), stamp))
        c.execute('INSERT INTO live_judgments VALUES(?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET category=excluded.category,manual_fields=excluded.manual_fields,reason=excluded.reason,version=excluded.version,updated_at=excluded.updated_at',
                  (message_id, category, json.dumps(fields, ensure_ascii=False), reason, version, stamp))
        # Human review can deliberately admit an excluded record; its original
        # filter outcome and lack of contact permission stay unchanged.
        attach(c, message_id, allow_filtered=True)
        app.event(c, 'live_review', f'人工核对弹幕 #{message_id}；未发送消息')
    return {'saved': True, 'id': message_id}
