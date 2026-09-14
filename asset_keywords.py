"""Continuously observed vocabulary, activated by an auditable human/model review."""
import json
import re
import clubops as app

SCHEMA='''CREATE TABLE IF NOT EXISTS asset_keywords (
 term TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',kind TEXT NOT NULL DEFAULT 'service',scope TEXT NOT NULL DEFAULT 'asset',
 reason TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_keyword_sources (
 term TEXT NOT NULL,asset_key TEXT NOT NULL,title TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
 PRIMARY KEY(term,asset_key)
);
CREATE TABLE IF NOT EXISTS asset_keyword_reviews (
 id INTEGER PRIMARY KEY,term TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,kind TEXT NOT NULL,scope TEXT NOT NULL,
 reason TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keyword_model_reviews (
 id INTEGER PRIMARY KEY,term TEXT NOT NULL,input_hash TEXT NOT NULL,engine TEXT NOT NULL,
 status TEXT NOT NULL,input_json TEXT NOT NULL,result_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,finished_at TEXT,UNIQUE(term,input_hash)
);'''


def observe(c,key,title,tags=()):
    # Durable distinct assets, not repeated poll counts or synthetic phrases.
    terms=list(dict.fromkeys([*re.findall(r'[#＃]([^\s#＃]{2,30})',title or ''),*tags]))[:30]
    for term in terms:
        if not isinstance(term,str) or not 2<=len(term.strip())<=30:continue
        term=term.strip();stamp=app.now()
        c.execute('INSERT OR IGNORE INTO asset_keywords(term,created_at,updated_at) VALUES(?,?,?)',(term,stamp,stamp))
        c.execute('''INSERT INTO asset_keyword_sources VALUES(?,?,?,?,?) ON CONFLICT(term,asset_key)
          DO UPDATE SET title=excluded.title,last_seen_at=excluded.last_seen_at''',(term,key,title[:5000],stamp,stamp))


def active(c,scope='asset'):
    words={'game':set(),'service':set(),'search':set()}
    for r in c.execute("SELECT term,kind FROM asset_keywords WHERE status='approved' AND scope IN (?, 'both')",(scope,)):words[r['kind']].add(r['term'])
    # References are the authority for their terms; withdrawal removes inherited activation.
    for r in c.execute("SELECT rule FROM asset_references WHERE status='approved' AND ?='asset'",(scope,)):
        rule=json.loads(r[0]);words['game'].update(rule['game_terms']);words['service'].update(rule['service_terms']);words['search'].update(rule.get('queries',[]))
    return {k:sorted(v) for k,v in words.items()}


def literal_hits(text,terms):
    folded=(text or '').casefold();return [t for t in terms if t.casefold() in folded]


def message_relevance(c,text,title='',parent=''):
    from intent_rules import companion_relevance
    result=companion_relevance(text,title,parent)
    if result.get('excluded_teamup'):return result
    hits=literal_hits(text,active(c,'message')['service'])
    if not hits:return result
    return {**result,'passed':True,'learned_keywords':hits,
            'reason':'原文命中已评审评论初筛词；是否有客户意图仍需另行判断。',
            'evidence':[dict(kind='reviewed_keyword',source='comment',text=term) for term in hits[:4]]}


def backfill(c):
    import asset_references as refs
    for row in c.execute('SELECT DISTINCT external_id FROM videos').fetchall():
        content=refs.work(c,row[0])
        if content:observe(c,'work:'+row[0],content['copy'],content['tags'])
    for row in c.execute('SELECT room_url,title FROM live_rooms').fetchall():observe(c,'live:'+row[0],row[1])


def state(mode='live',q='',scope='all',status='all'):
    if scope not in ('all','asset','message') or status not in ('all','active','pending'):raise ValueError('词库筛选无效')
    with app.LOCKS[mode],app.db(mode) as c:
        words=active(c);messages=active(c,'message');inherited={t:k for k in ('game','service','search') for t in words[k]+messages[k]}
        # Brief summaries only. Evidence is fetched when opening a review.
        rows={r['term']:dict(r) for r in c.execute('''SELECT k.*,COUNT(s.asset_key) AS source_count,MAX(s.last_seen_at) AS last_seen_at
          FROM asset_keywords k LEFT JOIN asset_keyword_sources s ON s.term=k.term GROUP BY k.term''')}
        for r in c.execute('''SELECT term,COUNT(*) AS total,SUM(current) AS usable,MAX(last_seen_at) AS last_seen
                             FROM comment_keyword_sources GROUP BY term'''):
            if r['term'] not in rows:continue
            row=rows[r['term']];row['comment_source_count']=r['total'];row['current_comment_source_count']=r['usable']
            row['source_count']+=r['total'];row['last_seen_at']=max(row['last_seen_at'] or '',r['last_seen'])
        for r in c.execute('''SELECT r.term,r.status,r.result_json FROM keyword_model_reviews r
            WHERE r.id=(SELECT MAX(p.id) FROM keyword_model_reviews p WHERE p.term=r.term)'''):
            if r['term'] in rows:
                rows[r['term']]['model_review_status']=r['status']
                rows[r['term']]['model_review_reason']=json.loads(r['result_json']).get('reason','')
        for term,kind in inherited.items():
            if term not in rows:rows[term]=dict(term=term,kind=kind,scope='asset',status='reference',reason='来自已评审参考资产',revision=0,source_count=0,last_seen_at=None)
            rows[term]['active']=True;rows[term]['active_kind']=kind
        if scope!='all':
            active_terms={t for terms in active(c,scope).values() for t in terms}
            rows={term:r for term,r in rows.items() if r['scope'] in (scope,'both') or
                  term in active_terms or (scope=='message' and r.get('comment_source_count'))}
            for term,row in rows.items():row['active']=term in active_terms
        values=sorted(rows.values(),key=lambda r:(not r.get('active'),r['status']!='pending',-r['source_count'],r['term']))
        counts=dict(total=len(values),active=sum(bool(r.get('active')) for r in values),pending=sum(r['status']=='pending' and not r.get('active') for r in values))
    query=str(q).strip().casefold()[:80];filtered=[r for r in values if (not query or query in r['term'].casefold()) and
        (status=='all' or (bool(r.get('active')) if status=='active' else r['status']=='pending' and not r.get('active')))]
    return dict(rows=filtered[:500],counts=counts,matched=len(filtered),limit=500)


def detail(term,mode='live'):
    with app.LOCKS[mode],app.db(mode) as c:
        row=c.execute('SELECT * FROM asset_keywords WHERE term=?',(term,)).fetchone()
        if not row:raise ValueError('该词由参考资产管理，请在参考评审中修改')
        inherited=[]
        for r in c.execute("SELECT id,rule FROM asset_references WHERE status='approved'"):
            rule=json.loads(r['rule'])
            if term in rule['game_terms']+rule['service_terms']+rule.get('queries',[]):inherited.append(r['id'])
        return dict(**dict(row),reference_ids=inherited,
            model_reviews=[dict(r) for r in c.execute('SELECT id,status,result_json,created_at,finished_at FROM keyword_model_reviews WHERE term=? ORDER BY id DESC LIMIT 3',(term,))],
            sources=[dict(r) for r in c.execute('SELECT asset_key,title,last_seen_at FROM asset_keyword_sources WHERE term=? ORDER BY last_seen_at DESC LIMIT 5',(term,))],
            comment_sources=[dict(r) for r in c.execute('''SELECT s.*,x.external_id AS comment_external_id,v.url AS work_url
              FROM comment_keyword_sources s JOIN comments x ON x.id=s.comment_id JOIN videos v ON v.id=x.video_id
              WHERE s.term=? ORDER BY s.current DESC,s.last_seen_at DESC LIMIT 5''',(term,))])


def propose(body,mode='live'):
    if mode!='live' or set(body) not in ({'term'},{'term','scope'}):raise ValueError('词库参数无效')
    scope=body.get('scope','asset')
    if scope not in ('asset','message','both'):raise ValueError('词库用途无效')
    term=body['term'].strip() if isinstance(body['term'],str) else ''
    if not 2<=len(term)<=30:raise ValueError('关键词为 2–30 字')
    with app.LOCKS[mode],app.db(mode) as c:
        c.execute('INSERT OR IGNORE INTO asset_keywords(term,scope,created_at,updated_at) VALUES(?,?,?,?)',(term,scope,app.now(),app.now()))
    return detail(term,mode)


def review(body,mode='live'):
    if mode!='live' or set(body)!={'term','revision','status','kind','scope','reason'}:raise ValueError('词库评审参数无效')
    if body['status'] not in ('approved','rejected','pending') or body['kind'] not in ('game','service','search'):raise ValueError('词库分类无效')
    if body['scope'] not in ('asset','message','both'):raise ValueError('词库用途无效')
    if body['scope']!='asset' and body['kind']!='service':raise ValueError('评论与弹幕初筛仅接受明确的陪玩服务词')
    reason=str(body['reason']).strip()
    if not 4<=len(reason)<=1000:raise ValueError('请填写 4–1000 字的评审理由')
    with app.LOCKS[mode],app.db(mode) as c:
        row=c.execute('SELECT * FROM asset_keywords WHERE term=?',(body['term'],)).fetchone()
        if not row or row['revision']!=body['revision']:raise ValueError('词条已更新，请重新打开')
        revision=row['revision']+1;stamp=app.now()
        c.execute('UPDATE asset_keywords SET status=?,kind=?,scope=?,reason=?,revision=?,updated_at=? WHERE term=?',
            (body['status'],body['kind'],body['scope'],reason,revision,stamp,row['term']))
        c.execute('INSERT INTO asset_keyword_reviews(term,revision,status,kind,scope,reason,created_at) VALUES(?,?,?,?,?,?,?)',
            (row['term'],revision,body['status'],body['kind'],body['scope'],reason,stamp))
        if body['status']=='approved' and body['kind']!='game' and body['scope']!='message':
            from video_discovery import GAME_PATTERN
            query=row['term'] if body['kind']=='search' and GAME_PATTERN.search(row['term']) else '无畏契约 '+row['term']
            c.execute('INSERT OR IGNORE INTO discovery_queries(keyword,next_check_at) VALUES(?,?)',(query,stamp))
        import asset_verticality
        asset_verticality.backfill(c)
    return state(mode)


def queries(c):
    from video_discovery import GAME_PATTERN
    from game_scope import in_pc_scope
    words=active(c)
    return sorted({q for t in words['search']+words['service'] if in_pc_scope(q:=t if GAME_PATTERN.search(t) else '无畏契约 '+t)})
