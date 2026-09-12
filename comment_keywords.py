"""Local, source-bound comment phrase candidates. Observation never approves a word."""
import json
import re
import clubops as app
import analysis_store as store
from intent_rules import COMPANION, SEPARATOR

SCHEMA = '''CREATE TABLE IF NOT EXISTS comment_keyword_sources (
 term TEXT NOT NULL,comment_id INTEGER NOT NULL,input_hash TEXT NOT NULL,
 raw_text TEXT NOT NULL,category TEXT NOT NULL,method TEXT NOT NULL,reason TEXT NOT NULL,
 judgment_key TEXT NOT NULL,current INTEGER NOT NULL DEFAULT 1,
 first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,PRIMARY KEY(term,comment_id)
);
CREATE INDEX IF NOT EXISTS idx_comment_keyword_record ON comment_keyword_sources(comment_id);'''
POSITIVE = ('buyer', 'seller', 'recruit')


def phrases(text, result):
    """Literal short phrases only; no inferred synonym, title, contact ID or model call."""
    spans = []
    method = result['analysis_method']
    if method == 'model':
        spans = [e['text'] for e in result.get('facts', {}).get('evidence', [])
                 if e.get('kind') == 'category' and e.get('source') == 'comment'
                 and isinstance(e.get('text'), str) and e['text'] in text]
    elif method == 'human':
        spans = [text]
    else:
        spans = [part for part in SEPARATOR.split(text) if re.search(COMPANION, part, re.I)]
    candidates = []
    for span in spans:
        for part in SEPARATOR.split(span):
            part = part.strip(' \t\r\n“”‘’「」"\'')
            # Keep phrases with original wording; long comments and contact handles
            # need a reviewer to add a precise term instead of arbitrary n-grams.
            if not re.fullmatch(r'[\u3400-\u9fffA-Za-z ]{2,20}', part):
                continue
            if re.search(r'微信|微[信xX]|[vV][xX]|扣扣|[qQ]{2}|抖音号|手机号|电话|私信|加我|加你', part):
                continue
            candidates.append(part)
    return list(dict.fromkeys(candidates))[:6]


def observe(c, record_id, *, model_engine=None):
    source, row = store.inputs(c, 'comment', record_id)
    fingerprint = store.digest(source)
    c.execute('UPDATE comment_keyword_sources SET current=0 WHERE comment_id=?', (record_id,))
    result = dict(row)
    result['facts'] = json.loads(row['facts'])
    key = 'rules:' + store.digest([fingerprint, row['category'], row['reason']])
    if row['analysis_method'] == 'human':
        key = 'human:' + store.digest([fingerprint, row['category'], row['reason'], row['manual_fields']])
    elif row['analysis_method'] == 'pending':
        return
    else:
        model = store.latest(c, 'comment', record_id, 'model', fingerprint)
        if model and model['status'] == 'completed' and model['engine'] == model_engine:
            result = model['result']
            key = 'model:' + str(model['id'])
    if result['category'] not in POSITIVE:
        return
    for term in phrases(source['text'], result):
        stamp = app.now()
        # Existing asset words retain their reviewed scope; combining scopes is
        # an explicit review, never a side effect of seeing a comment.
        c.execute("INSERT OR IGNORE INTO asset_keywords(term,scope,created_at,updated_at) VALUES(?,'message',?,?)", (term, stamp, stamp))
        c.execute('''INSERT INTO comment_keyword_sources VALUES(?,?,?,?,?,?,?,?,1,?,?)
          ON CONFLICT(term,comment_id) DO UPDATE SET input_hash=excluded.input_hash,
          raw_text=excluded.raw_text,category=excluded.category,method=excluded.method,
          reason=excluded.reason,judgment_key=excluded.judgment_key,current=1,last_seen_at=excluded.last_seen_at''',
          (term, record_id, fingerprint, source['text'], result['category'], result['analysis_method'],
           result.get('reason', ''), key, stamp, stamp))


def backfill(c):
    import semantic
    engine = semantic.state()['engine']
    for row in c.execute("SELECT id FROM comments WHERE analysis_method!='pending'").fetchall():
        observe(c, row['id'], model_engine=engine)


def invalidate(c, record_id):
    c.execute('UPDATE comment_keyword_sources SET current=0 WHERE comment_id=?', (record_id,))
