"""Reviewed local discovery assets. Literal paired terms, never automatic intent."""
import json
import re
import clubops as app

SCHEMA='''CREATE TABLE IF NOT EXISTS asset_references (
 id INTEGER PRIMARY KEY,asset_key TEXT NOT NULL UNIQUE,content TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',rule TEXT NOT NULL DEFAULT '{}',
 reason TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_reference_reviews (
 id INTEGER PRIMARY KEY,reference_id INTEGER NOT NULL,revision INTEGER NOT NULL,
 status TEXT NOT NULL,content TEXT NOT NULL,rule TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_content (
 asset_key TEXT PRIMARY KEY,content TEXT NOT NULL,updated_at TEXT NOT NULL
);'''


def tags(text):
    return list(dict.fromkeys(re.findall(r'[#＃]([^\s#＃]{1,40})',text or '')))[:30]


def content_from_row(row):
    caption=str(row.get('video_title') or '')[:5000]
    extra=row.get('tags') or []
    extra=[t for t in extra if isinstance(t,str) and 0<len(t)<=40][:30] if isinstance(extra,list) else []
    return dict(title=caption,copy=caption,tags=list(dict.fromkeys(tags(caption)+extra))[:30],
        author_name=str(row.get('author_nickname') or '')[:120],author_sec_uid=row.get('author_sec_uid') or '',
        source_url='https://www.douyin.com/video/'+str(row['video_id']) if re.fullmatch(r'[0-9]{5,30}',str(row.get('video_id',''))) else '',
        source_note='平台作品描述；标题与文案未被平台分开提供。')


def save_content(c,row):
    key=row.get('video_id')
    if not key or not row.get('video_title'):return
    content=content_from_row(row)
    import asset_keywords
    asset_keywords.observe(c,'work:'+key,content['copy'],content['tags'])
    old=c.execute('SELECT content FROM work_content WHERE asset_key=?',(key,)).fetchone()
    if old:
        previous=json.loads(old[0])
        for field in ('author_name','author_sec_uid'):
            if not content[field]:content[field]=previous.get(field,'')
        content['tags']=list(dict.fromkeys(content['tags']+previous.get('tags',[])))[:30]
    c.execute('''INSERT INTO work_content VALUES(?,?,?) ON CONFLICT(asset_key)
      DO UPDATE SET content=excluded.content,updated_at=excluded.updated_at''',
      (key,json.dumps(content,ensure_ascii=False),app.now()))


def work(c,key):
    row=c.execute('SELECT content FROM work_content WHERE asset_key=?',(key,)).fetchone()
    if row:return json.loads(row[0])
    row=c.execute('''SELECT w.title AS video_title,w.author_sec_uid,a.nickname AS author_nickname
      FROM discovery_works w LEFT JOIN discovery_authors a ON a.sec_uid=w.author_sec_uid WHERE w.video_id=?''',(key,)).fetchone()
    if row:return content_from_row(dict(row,video_id=key))
    row=c.execute('SELECT title AS video_title FROM videos WHERE external_id=? ORDER BY id LIMIT 1',(key,)).fetchone()
    return content_from_row(dict(row,video_id=key)) if row else None


def approved(c):
    return [dict(id=r['id'],revision=r['revision'],content=json.loads(r['content']),rule=json.loads(r['rule']),reason=r['reason'])
            for r in c.execute("SELECT * FROM asset_references WHERE status='approved' ORDER BY id")]


def match(text,references):
    text=text.casefold();hits=[]
    for r in references:
        game=[x for x in r['rule']['game_terms'] if x.casefold() in text]
        service=[x for x in r['rule']['service_terms'] if x.casefold() in text]
        if game and service:hits.append(dict(id=r['id'],revision=r['revision'],game_terms=game,service_terms=service,reason=r['reason']))
    return hits[:10]


def discovery_assets(c):
    refs=approved(c)
    authors={r['content']['author_sec_uid'] for r in refs if r['rule'].get('focus_author') and r['content'].get('author_sec_uid')}
    import asset_keywords
    queries=asset_keywords.queries(c)
    return authors,queries


def state(mode='live'):
    with app.LOCKS[mode],app.db(mode) as c:
        counts=dict(c.execute('SELECT status,COUNT(*) FROM asset_references GROUP BY status'))
        rows=[dict(r) for r in c.execute('SELECT * FROM asset_references ORDER BY updated_at DESC,id DESC LIMIT 200')]
        for r in rows:r['content']=json.loads(r['content']);r['rule']=json.loads(r['rule'])
        authors,queries=discovery_assets(c)
    return dict(rows=rows,counts=counts,focused_authors=len(authors),queries=queries,limit=200)


def propose(body,mode='live'):
    if mode!='live':raise ValueError('参考资产仅在正式工作区管理')
    if set(body)!={'asset_key'}:raise ValueError('从已保存的作品创建评审样本')
    with app.LOCKS[mode],app.db(mode) as c:
        key=str(body['asset_key']);content=work(c,key)
        if not content:raise ValueError('作品不存在')
        c.execute('''INSERT OR IGNORE INTO asset_references(asset_key,content,created_at,updated_at)
          VALUES(?,?,?,?)''',('work:'+key,json.dumps(content,ensure_ascii=False),app.now(),app.now()))
    return state(mode)


def review(body,mode='live'):
    if mode!='live':raise ValueError('参考资产仅在正式工作区管理')
    if set(body)!={'id','revision','status','reason','game_terms','service_terms','focus_author'}:raise ValueError('评审字段不完整')
    if body['status'] not in ('approved','rejected','pending') or type(body['focus_author']) is not bool:raise ValueError('评审状态无效')
    reason=str(body['reason']).strip()
    if not 4<=len(reason)<=1000:raise ValueError('请填写 4–1000 字的评审理由')
    with app.LOCKS[mode],app.db(mode) as c:
        row=c.execute('SELECT * FROM asset_references WHERE id=?',(body['id'],)).fetchone()
        if not row or row['revision']!=body['revision']:raise ValueError('样本已更新，请重新打开评审')
        content=json.loads(row['content']);text=' '.join([content['title'],content['copy'],*content['tags']]).casefold()
        terms={}
        for field in ('game_terms','service_terms'):
            values=body[field]
            if not isinstance(values,list) or len(values)>6 or any(not isinstance(x,str) or not 2<=len(x.strip())<=30 or x.strip().casefold() not in text for x in values):
                raise ValueError('参考词须来自样本原文，每组最多 6 个，单词 2–30 字')
            terms[field]=list(dict.fromkeys(x.strip() for x in values))
        if body['status']=='approved' and (not all(terms.values()) or set(terms['game_terms'])&set(terms['service_terms'])):
            raise ValueError('通过时需分别提供游戏词与陪玩服务词，不得使用同一词同时充当两种依据')
        # Author identity must already be bound by collected platform data.
        sec=content.get('author_sec_uid')
        if body['focus_author'] and (not sec or not c.execute('SELECT 1 FROM discovery_authors WHERE sec_uid=?',(sec,)).fetchone()):
            raise ValueError('尚未核实作者标识，不能仅凭昵称绑定作者')
        from video_discovery import GAME_PATTERN
        queries=[g+' '+s for g in terms['game_terms'] for s in terms['service_terms'] if GAME_PATTERN.search(g+' '+s)][:2]
        rule={**terms,'focus_author':body['focus_author'],'queries':queries}
        raw=json.dumps(rule,ensure_ascii=False);revision=row['revision']+1;stamp=app.now()
        c.execute('UPDATE asset_references SET status=?,rule=?,reason=?,revision=?,updated_at=? WHERE id=?',
            (body['status'],raw,reason,revision,stamp,row['id']))
        c.execute('INSERT INTO asset_reference_reviews(reference_id,revision,status,content,rule,reason,created_at) VALUES(?,?,?,?,?,?,?)',
            (row['id'],revision,body['status'],row['content'],raw,reason,stamp))
        import asset_keywords
        for q in asset_keywords.queries(c):
            if body['status']=='approved':c.execute('INSERT OR IGNORE INTO discovery_queries(keyword,next_check_at) VALUES(?,?)',(q,stamp))
        import asset_verticality
        asset_verticality.backfill(c)
    return state(mode)
