"""Local, evidence-backed asset classification and model routing.

Asset relevance never proves that a particular commenter is a customer.
"""
import hashlib
import json
import re

import clubops as app
import asset_references as references
import asset_keywords as vocabulary
from intent_rules import COMPANION, companion_relevance
from video_discovery import GAME_PATTERN
from game_scope import exclusion_reason, record_exclusion

VERSION='asset-verticality-v6-cn-pc'
SAMPLE_LIMIT=300
SERVICE_PATTERN=re.compile(COMPANION+r'|陪同上分|付费教学|有偿教学|复盘接单|陪练接单',re.I)
SCHEMA='''CREATE TABLE IF NOT EXISTS asset_verticality (
 kind TEXT NOT NULL,asset_key TEXT NOT NULL,title TEXT NOT NULL,input_hash TEXT NOT NULL,
 result TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(kind,asset_key)
);'''


def classify(title,history,*,game_category=False,reference_rules=(),tags=(),words=None):
    """Explicit title pair, or repeated service evidence in a game-scoped sample."""
    title=' '.join([title or '',*tags])
    reference_hits=references.match(title,reference_rules)
    words=words or {'game':[],'service':[]}
    game_hits=list(dict.fromkeys(m.group() for m in GAME_PATTERN.finditer(title)))[:6]
    service_hits=list(dict.fromkeys(m.group() for m in SERVICE_PATTERN.finditer(title)))[:6]
    game_hits=list(dict.fromkeys(game_hits+vocabulary.literal_hits(title,words['game'])))[:12]
    service_hits=list(dict.fromkeys(service_hits+vocabulary.literal_hits(title,words['service'])))[:12]
    unique=[];seen=set()
    for row in history:
        text=(row.get('text') or '').strip()
        if exclusion_reason(text,title,'无畏契约' if game_category else ''):continue
        key=re.sub(r'\s+','',text).casefold()
        if not key or key in seen:continue
        seen.add(key);unique.append(row)
        if len(unique)>=SAMPLE_LIMIT:break
    matches=[];game_count=0
    for row in unique:
        text=row['text'];game_count+=bool(GAME_PATTERN.search(text) or vocabulary.literal_hits(text,words['game']))
        terms=list(dict.fromkeys(m.group() for m in SERVICE_PATTERN.finditer(text)))[:6]
        terms=list(dict.fromkeys(terms+vocabulary.literal_hits(text,words['service'])))[:12]
        if terms:matches.append(dict(id=row.get('id',''),text=text[:160],keywords=terms))
    count=len(unique);hits=len(matches);ratio=round(hits/count,4) if count else 0
    game=bool(game_hits or game_category or game_count>=3 and game_count/max(count,1)>=.1)
    title_match=bool(game and service_hits)
    history_match=bool(game and hits>=3 and ratio>=.1)
    matched=bool(title_match or history_match or reference_hits)
    if reference_hits:reason='标题、文案或标签命中已评审参考资产中的游戏词与陪玩服务词组合。'
    elif title_match:reason='游戏范围与标题、文案或标签中的陪玩服务关键词同时匹配。'
    elif history_match:reason=f'游戏范围已确认，历史去重文字中 {hits}/{count} 条命中陪玩服务关键词。'
    elif not game:reason='当前标题和历史样本不足以确认无畏契约范围。'
    elif not count:reason='属于无畏契约范围，但标题没有陪玩服务证据，尚无历史文字样本。'
    else:reason=f'当前陪玩服务证据不足：历史去重文字 {hits}/{count} 条命中，未同时达到 3 条且占比 10%。'
    excluded=exclusion_reason(title,'无畏契约' if game_category else '')
    if excluded:matched=False;game=False;reason=excluded;reference_hits=[]
    return dict(version=VERSION,matched=matched,label='垂直对口' if matched else '普通监控',reason=reason,
        game_confirmed=bool(game or reference_hits),game_category=bool(game_category),title_game_keywords=game_hits,
        title_service_keywords=service_hits,history_samples=count,history_hits=hits,history_ratio=ratio,
        evidence=matches[:5],sample_limit=SAMPLE_LIMIT,reference_hits=reference_hits,tags=list(tags),
        reference_only=bool(reference_hits and not (title_match or history_match)),scope_exclusion=excluded)


def work_history(c,key):
    # A max aggregate selects the text from the most recent immutable snapshot.
    rows=[dict(id=r['external_id'],text=r['comment_text']) for r in c.execute('''
      SELECT external_id,comment_text,MAX(task_id) AS latest FROM collection_observations
      WHERE kind='comment' AND page_url=? AND trim(comment_text)!=''
      GROUP BY external_id ORDER BY latest DESC,external_id LIMIT ?''',
      ('https://www.douyin.com/video/'+key,SAMPLE_LIMIT))]
    known={r['id'] for r in rows}
    for r in c.execute('''SELECT x.external_id,x.raw_text FROM comments x JOIN videos v ON v.id=x.video_id
      WHERE v.external_id=? ORDER BY x.id DESC LIMIT ?''',(key,SAMPLE_LIMIT)):
        if r['external_id'] not in known:rows.append(dict(id=r['external_id'],text=r['raw_text']));known.add(r['external_id'])
    return rows


def refresh(c,kind,key,*,reference_rules=None,words=None):
    if kind=='work':
        row=c.execute('SELECT title FROM videos WHERE external_id=? ORDER BY id LIMIT 1',(key,)).fetchone()
        if not row:return None
        content=references.work(c,key) or {};title=content.get('copy') or row['title']
        content_tags=content.get('tags',[]);history=work_history(c,key);category=False
    elif kind=='live':
        row=c.execute('SELECT title,source FROM live_rooms WHERE room_url=?',(key,)).fetchone()
        title=row['title'] if row else '';category=bool(row and row['source']=='valorant_category');content_tags=references.tags(title)
        history=[dict(id=str(r['id']),text=r['raw_text']) for r in c.execute('''SELECT m.id,m.raw_text
          FROM live_messages m JOIN live_sessions s ON s.id=m.session_id
          WHERE s.room_url=? ORDER BY m.id DESC LIMIT ?''',(key,SAMPLE_LIMIT))]
    else:raise ValueError('资产类型无效')
    rules=references.approved(c) if reference_rules is None else reference_rules
    words=vocabulary.active(c) if words is None else words
    fingerprint=hashlib.sha256(json.dumps([VERSION,title,category,history,content_tags,rules,words],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    old=c.execute('SELECT input_hash,result FROM asset_verticality WHERE kind=? AND asset_key=?',(kind,key)).fetchone()
    if old and old['input_hash']==fingerprint:return json.loads(old['result'])
    result=classify(title,history,game_category=category,reference_rules=rules,tags=content_tags,words=words)
    c.execute('''INSERT INTO asset_verticality(kind,asset_key,title,input_hash,result,updated_at) VALUES(?,?,?,?,?,?)
      ON CONFLICT(kind,asset_key) DO UPDATE SET title=excluded.title,input_hash=excluded.input_hash,
      result=excluded.result,updated_at=excluded.updated_at''',
      (kind,key,title,fingerprint,json.dumps(result,ensure_ascii=False),app.now()))
    return result


def backfill(c):
    rules=references.approved(c);words=vocabulary.active(c)
    for r in c.execute('SELECT DISTINCT external_id FROM videos').fetchall():refresh(c,'work',r[0],reference_rules=rules,words=words)
    for r in c.execute('SELECT room_url FROM live_rooms UNION SELECT room_url FROM live_sessions').fetchall():refresh(c,'live',r[0],reference_rules=rules,words=words)


def profiles(c,kind):
    return {r['asset_key']:json.loads(r['result']) for r in c.execute('SELECT asset_key,result FROM asset_verticality WHERE kind=?',(kind,))}


def routing(c,kind,record_id,*,refresh_asset=False):
    if kind=='group':
        import group_monitor
        return group_monitor.routing(c,record_id)
    if kind=='comment':
        row=c.execute('''SELECT x.raw_text,x.video_id,v.external_id AS asset_key,v.title,'' AS filter_reason
          FROM comments x JOIN videos v ON v.id=x.video_id WHERE x.id=?''',(record_id,)).fetchone()
        asset_kind='work'
    elif kind=='live':
        row=c.execute('''SELECT m.raw_text,s.room_url AS asset_key,COALESCE(r.title,'') AS title,m.filter_reason
          FROM live_messages m JOIN live_sessions s ON s.id=m.session_id
          LEFT JOIN live_rooms r ON r.room_url=s.room_url WHERE m.id=?''',(record_id,)).fetchone()
        asset_kind='live'
    else:raise ValueError('原文类型无效')
    if not row:raise ValueError('原文不存在')
    excluded=record_exclusion(c,kind,record_id)
    profile=refresh(c,asset_kind,row['asset_key']) if refresh_asset else None
    if profile is None:
        stored=c.execute('SELECT result FROM asset_verticality WHERE kind=? AND asset_key=?',(asset_kind,row['asset_key'])).fetchone()
        profile=json.loads(stored[0]) if stored else classify(row['title'],[])
    keyword=vocabulary.message_relevance(c,row['raw_text'],row['title'])
    learned=keyword.get('learned_keywords',[])
    allowed=bool(profile['matched'] and not row['filter_reason'] and (kind=='comment' or keyword['passed']))
    reason=('垂直作品的新评论直接分析意图。' if kind=='comment' else '垂直直播间弹幕通过陪玩关键词匹配。') if allowed else (
        '非垂直对口资产，只做关键词匹配。' if not profile['matched'] else
        '弹幕未通过本批采集关键词配置。' if row['filter_reason'] else '垂直直播间弹幕未命中陪玩关键词。')
    if excluded:allowed=False;reason=excluded
    return dict(version=VERSION,model_allowed=allowed,route='model' if allowed else 'keywords',reason=reason,
                asset_kind=asset_kind,asset_key=row['asset_key'],asset=profile,keyword_match=keyword if kind=='live' else None,learned_keywords=learned)
