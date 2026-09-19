"""Local incident postmortems. Similar symptoms suggest cases; they never execute remedies."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re


def initialize(c):
    c.executescript('''
      CREATE TABLE IF NOT EXISTS incident_symptoms(
        incident_id TEXT PRIMARY KEY, symptom_key TEXT NOT NULL, episode_id TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS symptom_lookup ON incident_symptoms(symptom_key);
      CREATE TABLE IF NOT EXISTS incident_reviews(
        incident_id TEXT PRIMARY KEY, cause_key TEXT, disposition TEXT NOT NULL,
        revision INTEGER NOT NULL, recorded_at REAL NOT NULL, report TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS incident_review_history(
        incident_id TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at REAL NOT NULL,
        report TEXT NOT NULL, PRIMARY KEY(incident_id,revision));
    ''')
    # Old records have no evidence of continuity. Do not infer confirmed recurrences.
    for row in c.execute("SELECT * FROM incidents WHERE channel!='self_test' AND id NOT IN (SELECT incident_id FROM incident_symptoms)").fetchall():
        link(c, row)


def symptom_key(channel, metadata):
    value = dict(channel=channel, reason=metadata.get('reason'), task_status=metadata.get('task_status'))
    if 'items' in metadata:
        value['statuses'] = sorted(set(str(i).rsplit(':', 1)[-1] for i in metadata['items']))
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def link(c, row, previous=None):
    """A task ID change during uninterrupted channel failure is not a second episode."""
    episode = row['id']
    if previous:
        found = c.execute('SELECT episode_id FROM incident_symptoms WHERE incident_id=?', (previous['id'],)).fetchone()
        if found:
            episode = found[0]
    c.execute('INSERT OR IGNORE INTO incident_symptoms VALUES(?,?,?)',
              (row['id'], symptom_key(row['channel'], json.loads(row['metadata'])), episode))


def related(c, incident):
    key = symptom_key(incident['channel'], json.loads(incident['metadata']))
    occurrences = c.execute('SELECT COUNT(DISTINCT episode_id) FROM incident_symptoms WHERE symptom_key=?', (key,)).fetchone()[0]
    rows = c.execute('''SELECT r.incident_id,r.recorded_at,r.report FROM incident_reviews r
        JOIN incident_symptoms s ON s.incident_id=r.incident_id
        WHERE s.symptom_key=? AND r.incident_id!=? ORDER BY r.recorded_at DESC LIMIT 5''',
        (key, incident.get('id', '') if isinstance(incident, dict) else incident['id'])).fetchall()
    return dict(similar_episodes=occurrences, cases=[dict(incident_id=r['incident_id'], **json.loads(r['report'])) for r in rows])


def evidence(root, items, *, required=False):
    if not isinstance(items, list) or len(items) > 12 or (required and not items):
        raise ValueError('Evidence must contain 1-12 existing artifact paths' if required else 'Invalid evidence list')
    checked = []
    for item in items:
        if not isinstance(item, dict) or set(item) - {'path', 'result'}:
            raise ValueError('Evidence accepts only path and result')
        name = item.get('path', '')
        if not isinstance(name, str):
            raise ValueError('Invalid evidence path')
        path = root / name
        resolved = path.resolve()
        if not isinstance(name, str) or Path(name).is_absolute() or not resolved.is_relative_to((root/'artifacts').resolve()) or not resolved.is_file():
            raise ValueError('Evidence must be an existing project artifact, not a private data file')
        if resolved.stat().st_size > 8_000_000:
            raise ValueError('Evidence file too large')
        checked.append(dict(path=resolved.relative_to(root.resolve()).as_posix(), sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(), result=item.get('result')))
    return checked


def validate(root, record):
    allowed = {'incident_id','cause_key','kind','disposition','root_cause','match_conditions','remedy',
               'prevention','next_action','regressions','runtime_evidence','changes'}
    if not isinstance(record, dict) or set(record) - allowed:
        raise ValueError('Invalid postmortem fields')
    kind, disposition = record.get('kind'), record.get('disposition')
    if kind not in ('software','external','unknown') or disposition not in ('fixed','mitigated','investigating','needs_user'):
        raise ValueError('Invalid root-cause kind or disposition')
    cause = record.get('cause_key')
    if cause is not None and (not isinstance(cause, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{3,79}', cause)):
        raise ValueError('Use a stable cause key; never use a task ID or raw exception')
    if kind == 'unknown' and cause is not None:
        raise ValueError('An unknown cause must not be assigned a confirmed cause key')
    if kind != 'unknown' and not cause:
        raise ValueError('A confirmed cause requires a stable cause key')
    if disposition == 'fixed' and kind != 'software':
        raise ValueError('External/unknown failures cannot be marked as a repaired software defect')
    value = {k:record.get(k) for k in ('cause_key','kind','disposition')}
    for key in ('root_cause','match_conditions','remedy','prevention','next_action'):
        text = record.get(key, '')
        if not isinstance(text, str) or len(text) > 2500 or (key != 'next_action' and len(text.strip()) < 8):
            raise ValueError('A concrete '+key+' is required (8-2500 characters)')
        if re.search(r'(?i)(?:cookie|authorization|csrf|api[_-]?key|access[_-]?token)\s*[:=]', text):
            raise ValueError('Do not put credentials in the fault knowledge base')
        value[key] = text.strip()
    if disposition != 'fixed' and len(value['next_action']) < 8:
        raise ValueError('Unrepaired causes must retain a concrete next action')
    value['regressions'] = evidence(root, record.get('regressions', []), required=disposition == 'fixed')
    if disposition == 'fixed' and any(r['result'] != 'passed' for r in value['regressions']):
        raise ValueError('Fixed defects require passing regression evidence')
    value['runtime_evidence'] = evidence(root, record.get('runtime_evidence', []), required=disposition in ('fixed','mitigated'))
    changes = record.get('changes', [])
    if not isinstance(changes, list) or len(changes) > 30 or (disposition == 'fixed' and not changes):
        raise ValueError('Fixed defects require changed source files')
    whitelist = set(json.loads((root/'source-files.json').read_text('utf-8')))
    if any(not isinstance(p, str) or p not in whitelist or not (root/p).is_file() for p in changes):
        raise ValueError('Changed source must belong to source-files.json')
    value['changes'] = [dict(path=p, sha256=hashlib.sha256((root/p).read_bytes()).hexdigest()) for p in changes]
    return value


def save(c, root, delivery_id, records, now):
    delivery = c.execute('SELECT incident_ids FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
    if not delivery:
        raise ValueError('Unknown delivery')
    ids = set(json.loads(delivery[0]))
    if not isinstance(records, list) or not records or len(records) > len(ids):
        raise ValueError('Supply postmortems for this delivery only')
    seen = set()
    for record in records:
        iid = record.get('incident_id') if isinstance(record, dict) else None
        if iid not in ids or iid in seen:
            raise ValueError('Duplicate or unrelated incident')
        seen.add(iid)
        incident = c.execute('SELECT * FROM incidents WHERE id=?', (iid,)).fetchone()
        if incident['channel'] == 'self_test':
            raise ValueError('Synthetic receipt tests must not become repair knowledge')
        value = validate(root, record)
        prior = c.execute('SELECT revision,report FROM incident_reviews WHERE incident_id=?', (iid,)).fetchone()
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if prior and prior['report'] == encoded:
            continue
        if value['cause_key'] and value['disposition'] == 'fixed':
            previous = c.execute('''SELECT r.report FROM incident_reviews r JOIN incident_symptoms s ON s.incident_id=r.incident_id
                WHERE r.cause_key=? AND r.disposition='fixed' AND s.episode_id!=(SELECT episode_id FROM incident_symptoms WHERE incident_id=?)
                ORDER BY r.recorded_at DESC LIMIT 1''', (value['cause_key'], iid)).fetchone()
            if previous and {r['sha256'] for r in value['regressions']} == {r['sha256'] for r in json.loads(previous[0])['regressions']}:
                raise ValueError('Confirmed recurrence requires a fresh regression run, not the old passing report')
        revision = prior['revision']+1 if prior else 1
        c.execute('INSERT INTO incident_review_history VALUES(?,?,?,?)', (iid,revision,now,encoded))
        c.execute('''INSERT INTO incident_reviews VALUES(?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET
            cause_key=excluded.cause_key,disposition=excluded.disposition,revision=excluded.revision,
            recorded_at=excluded.recorded_at,report=excluded.report''', (iid,value['cause_key'],value['disposition'],revision,now,encoded))


def require_recovery_record(c, rows, root):
    for row in rows:
        if row['channel'] == 'self_test':
            continue
        review = c.execute('SELECT disposition,report FROM incident_reviews WHERE incident_id=?', (row['id'],)).fetchone()
        if not review or review[0] not in ('fixed','mitigated'):
            raise ValueError('Record root cause, remedy, prevention and runtime evidence before resolving; unknown roots remain mitigated/open')
        value = json.loads(review['report'])
        for item in value['regressions']+value['runtime_evidence']+value['changes']:
            path = root/item['path']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('Evidence/source changed after the postmortem; verify and record again')


def listing(c, delivery_id=None):
    if delivery_id:
        row = c.execute('SELECT incident_ids FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
        if not row:
            raise ValueError('Unknown delivery')
        rows = [c.execute('SELECT * FROM incidents WHERE id=?', (i,)).fetchone() for i in json.loads(row[0])]
    else:
        rows = c.execute("SELECT * FROM incidents WHERE channel!='self_test' ORDER BY detected_at DESC LIMIT 30").fetchall()
    result = []
    for row in rows:
        review = c.execute('SELECT revision,report FROM incident_reviews WHERE incident_id=?', (row['id'],)).fetchone()
        result.append(dict(incident_id=row['id'], channel=row['channel'], state=row['state'], metadata=json.loads(row['metadata']),
            review=dict(revision=review['revision'], **json.loads(review['report'])) if review else None,
            **related(c,row)))
    causes = []
    for cause in c.execute('''SELECT r.cause_key,COUNT(DISTINCT s.episode_id) episodes FROM incident_reviews r
        JOIN incident_symptoms s ON s.incident_id=r.incident_id WHERE r.cause_key IS NOT NULL GROUP BY r.cause_key'''):
        latest = c.execute('SELECT disposition FROM incident_reviews WHERE cause_key=? ORDER BY recorded_at DESC,revision DESC LIMIT 1',(cause[0],)).fetchone()[0]
        causes.append(dict(cause_key=cause[0], confirmed_episodes=cause[1], confirmed_recurrences=max(0,cause[1]-1), disposition=latest))
    return dict(incidents=result, causes=causes)


def prompt_context(c, incidents):
    result = []
    for row in incidents:
        if row['channel'] == 'self_test':
            continue
        context = related(c,row)
        cases = [dict(incident_id=v['incident_id'],cause_key=v['cause_key'],disposition=v['disposition'],
            root_cause=v['root_cause'][:600],match_conditions=v['match_conditions'][:600],remedy=v['remedy'][:600],
            prevention=v['prevention'][:600],regressions=v['regressions']) for v in context['cases'][:3]]
        result.append(dict(channel=row['channel'],incident_id=row['id'] if 'id' in row.keys() else None,
            similar_episodes=context['similar_episodes'],reference_cases=cases))
    return json.dumps(result,ensure_ascii=False)
