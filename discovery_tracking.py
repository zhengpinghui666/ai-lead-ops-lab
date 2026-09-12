"""Persistent work/author discovery, dispatched through the existing monitor lock.

Catalog entries are observed candidates, not comments or customer judgments.
Author relevance ratios use author-feed samples, never biased search hits.
"""
from collections import deque
import json
import re
from datetime import datetime, timedelta

import clubops as app
import video_metadata
from video_discovery import GAME_PATTERN

SCHEMA = '''
CREATE TABLE IF NOT EXISTS discovery_authors (
 sec_uid TEXT PRIMARY KEY,nickname TEXT NOT NULL,seed_video_id TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1,priority TEXT NOT NULL DEFAULT 'auto',
 first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,last_checked_at TEXT,next_check_at TEXT
);
CREATE TABLE IF NOT EXISTS discovery_works (
 video_id TEXT PRIMARY KEY,author_sec_uid TEXT,title TEXT NOT NULL,published_at TEXT,
 relevant INTEGER NOT NULL,author_sample INTEGER NOT NULL DEFAULT 0,
 source_kind TEXT NOT NULL,source_target TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
 last_task_id INTEGER REFERENCES collection_tasks(id),enabled INTEGER NOT NULL DEFAULT 1,
 last_checked_at TEXT,next_check_at TEXT,quiet_streak INTEGER NOT NULL DEFAULT 0,new_recent_comments INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discovery_jobs (
 task_id INTEGER PRIMARY KEY REFERENCES collection_tasks(id),kind TEXT NOT NULL,key TEXT NOT NULL,
 config TEXT NOT NULL,settled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discovery_queries (keyword TEXT PRIMARY KEY,last_checked_at TEXT,next_check_at TEXT);
CREATE INDEX IF NOT EXISTS idx_discovery_author ON discovery_works(author_sec_uid,author_sample);
'''
DEFAULT = dict(enabled=False,keywords=['无畏契约陪玩','无畏契约陪练','瓦陪玩','无畏契约开黑','无畏契约复盘','VALORANT陪玩'],
    seed_videos=[],search_interval=300,author_interval=600,focus_interval=90,work_interval=60,
    focus_min_related=8,focus_ratio=80,initial_author_pages=3)

# Executed after the observation columns are migrated. Range access keeps old
# observations from making every scheduling turn scan the entire archive.
ACTIVITY_INDEX = '''CREATE INDEX IF NOT EXISTS idx_observation_published_activity
  ON collection_observations(julianday(published_at),page_url) WHERE kind='comment' '''
RECENT_ACTIVITY = '''SELECT page_url,
  strftime('%Y-%m-%dT%H:%M:%S+00:00',MAX(julianday(published_at))) AS latest_comment_at
  FROM collection_observations WHERE kind='comment'
  AND julianday(published_at)>=julianday(?,'-1 hour') AND julianday(published_at)<=julianday(?)
  AND julianday(published_at)<=julianday(observed_at) AND julianday(observed_at)<=julianday(?)
  AND trim(comment_text)!='' GROUP BY page_url'''


def config(c):
    row=c.execute("SELECT value FROM settings WHERE key='discovery_tracking'").fetchone()
    return {**DEFAULT,**(json.loads(row[0]) if row else {})}


def future(stamp,seconds):
    return (datetime.fromisoformat(stamp)+timedelta(seconds=seconds)).isoformat(timespec='seconds')


def paused_targets(c):
    import collector
    return {r['video_id'] for p in c.execute("SELECT target FROM collection_plans WHERE kind='video' AND status!='running'")
            for r in collector.video_targets(p[0])}


def author_rows(c,cfg):
    import asset_references
    reference_authors,_=asset_references.discovery_assets(c)
    rows=[dict(r) for r in c.execute('''SELECT a.*,COUNT(w.video_id) AS sampled,
      COALESCE(SUM(w.relevant),0) AS related,
      COALESCE(SUM(json_extract(v.result,'$.matched')=1),0) AS vertical FROM discovery_authors a
      LEFT JOIN discovery_works w ON w.author_sec_uid=a.sec_uid AND w.author_sample=1
      LEFT JOIN asset_verticality v ON v.kind='work' AND v.asset_key=w.video_id GROUP BY a.sec_uid''')]
    for r in rows:
        r['reference_focused']=r['sec_uid'] in reference_authors
        r['ratio']=round(r['related']/r['sampled']*100,1) if r['sampled'] else None
        r['vertical_ratio']=round(r['vertical']/r['sampled']*100,1) if r['sampled'] else None
        r['focused']=r['priority']=='focus' or r['priority']=='auto' and (r['reference_focused'] or r['vertical']>=cfg['focus_min_related'] and r['vertical']*100>=r['sampled']*cfg['focus_ratio'])
    return sorted(rows,key=lambda r:(not r['focused'],-r['vertical'],r['first_seen_at']))


def state(mode='live'):
    with app.LOCKS[mode],app.db(mode) as c:
        cfg=config(c);authors=author_rows(c,cfg)
        monitor=c.execute('SELECT status FROM collection_plans WHERE continuous=1').fetchone()
        enabled=cfg['enabled'] and bool(monitor and monitor['status']=='running') and mode=='live'
        counts=dict(c.execute('''SELECT COUNT(*) AS observed,COALESCE(SUM(relevant),0) AS related,
          COALESCE(SUM(relevant=1 AND enabled=1 AND last_checked_at IS NULL),0) AS awaiting,
          COALESCE(SUM(relevant=1 AND enabled=1 AND last_checked_at IS NOT NULL),0) AS watched FROM discovery_works''').fetchone())
        return dict(config=cfg,enabled=enabled,configured=cfg['enabled'],counts=counts,
            author_count=len(authors),focused_count=sum(r['focused'] and r['enabled'] for r in authors),
            authors=authors[:200],author_limit=200,
            queries=[dict(r) for r in c.execute('SELECT * FROM discovery_queries ORDER BY keyword') if r['keyword'] in cfg['keywords']])


def save(body,mode='live'):
    if mode!='live':raise ValueError('演示区不配置持续发现')
    import collector
    with collector.GUARD,app.LOCKS[mode],app.db(mode) as c:
        cfg={**config(c),**body}
        if set(body)-set(DEFAULT):raise ValueError('持续发现配置字段无效')
        if type(cfg['enabled']) is not bool:raise ValueError('启用状态无效')
        for key,low,high in [('search_interval',60,86400),('author_interval',60,86400),('focus_interval',30,86400),('work_interval',30,3600),('focus_min_related',3,100),('focus_ratio',50,100),('initial_author_pages',1,3)]:
            if type(cfg[key]) is not int or not low<=cfg[key]<=high:raise ValueError(f'{key} 超出允许范围')
        for key in ('keywords','seed_videos'):
            if not isinstance(cfg[key],list) or len(cfg[key])>(12 if key=='keywords' else 10):raise ValueError('发现入口数量超出上限')
            if any(not isinstance(v,str) for v in cfg[key]):raise ValueError('发现入口格式无效')
            cfg[key]=list(dict.fromkeys(v.strip() for v in cfg[key] if v.strip()))
        if not cfg['keywords'] or any(len(v)>80 or not GAME_PATTERN.search(v) for v in cfg['keywords']):raise ValueError('搜索词需包含无畏契约或明确游戏别名')
        if any(not re.fullmatch(r'[0-9]{5,30}',v) for v in cfg['seed_videos']):raise ValueError('作者种子需使用视频数字 ID')
        c.execute("INSERT INTO settings VALUES('discovery_tracking',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(cfg,ensure_ascii=False),))
        for keyword in cfg['keywords']:c.execute('INSERT OR IGNORE INTO discovery_queries(keyword,next_check_at) VALUES(?,?)',(keyword,app.now()))
        # Register existing game-scoped works, retaining explicit paused fixed targets.
        excluded=paused_targets(c)
        for row in c.execute('SELECT external_id,title FROM videos').fetchall():
            if re.fullmatch(r'[0-9]{5,30}',row[0]) and GAME_PATTERN.search(row[1]) and row[0] not in excluded:
                record(c,[dict(video_id=row[0],video_title=row[1])],source='registered',target='existing_work')
        app.event(c,'discovery','持续作品与作者发现配置已保存；运行服从评论监控总开关')
    return state(mode)


def author_command(body,mode='live'):
    if mode!='live' or set(body)!={'sec_uid','enabled','priority'} or type(body['enabled']) is not bool or body['priority'] not in ('auto','focus','normal'):
        raise ValueError('作者关注参数无效')
    import collector
    with collector.GUARD,app.LOCKS[mode],app.db(mode) as c:
        if not c.execute('SELECT 1 FROM discovery_authors WHERE sec_uid=?',(body['sec_uid'],)).fetchone():raise ValueError('作者不存在')
        c.execute('UPDATE discovery_authors SET enabled=?,priority=?,next_check_at=? WHERE sec_uid=?',
                  (body['enabled'],body['priority'],app.now(),body['sec_uid']))
    return state(mode)


def record(c,rows,*,source,target,task=None):
    """Only callers holding the DB lock pass verified worker rows or local seeds."""
    if task and (task['finished_at'] or task['status']=='cancelling'):return
    if not isinstance(rows,list) or len(rows)>50:raise ValueError('候选目录单次最多 50 项')
    stamp=app.now()
    source_row=c.execute("SELECT id FROM sources WHERE kind='browser' ORDER BY id LIMIT 1").fetchone()
    if not source_row:return
    paused=paused_targets(c)
    for row in rows:
        vid=row.get('video_id');title=row.get('video_title')
        if not isinstance(vid,str) or not re.fullmatch(r'[0-9]{5,30}',vid) or not isinstance(title,str):raise ValueError('发现作品字段无效')
        title=title[:5000];relevant=bool(GAME_PATTERN.search(title))
        old=c.execute('SELECT * FROM discovery_works WHERE video_id=?',(vid,)).fetchone()
        if not relevant and source!='author' and not old:continue
        sec=row.get('author_sec_uid') or ''
        if sec and (not isinstance(sec,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',sec)):raise ValueError('作者标识无效')
        if old and old['author_sec_uid'] and sec and old['author_sec_uid']!=sec:raise ValueError('同一作品的作者标识发生冲突')
        sec=sec or (old['author_sec_uid'] if old else '')
        published=app.timestamp(row['published_at']) if row.get('published_at') else None
        if sec:
            nickname=app.clean(row.get('author_nickname'),120) or '作者昵称待补全'
            c.execute('''INSERT INTO discovery_authors(sec_uid,nickname,seed_video_id,first_seen_at,last_seen_at,next_check_at)
              VALUES(?,?,?,?,?,?) ON CONFLICT(sec_uid) DO UPDATE SET last_seen_at=excluded.last_seen_at,
              nickname=CASE WHEN excluded.nickname!='作者昵称待补全' THEN excluded.nickname ELSE discovery_authors.nickname END''',
              (sec,nickname,vid,stamp,stamp,stamp))
        c.execute('''INSERT INTO discovery_works(video_id,author_sec_uid,title,published_at,relevant,author_sample,source_kind,source_target,first_seen_at,last_seen_at,last_task_id,next_check_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET author_sec_uid=COALESCE(excluded.author_sec_uid,discovery_works.author_sec_uid),
          title=excluded.title,published_at=COALESCE(excluded.published_at,discovery_works.published_at),relevant=excluded.relevant,
          author_sample=MAX(discovery_works.author_sample,excluded.author_sample),last_seen_at=excluded.last_seen_at,last_task_id=COALESCE(excluded.last_task_id,discovery_works.last_task_id)''',
          (vid,sec or None,title,published,int(relevant),int(source=='author'),source,target,stamp,stamp,task['id'] if task else None,stamp))
        if not old:
            previous=c.execute("SELECT t.finished_at FROM collection_checkpoints k JOIN collection_tasks t ON t.id=k.task_id WHERE k.video_id=? AND k.status='done' AND t.finished_at IS NOT NULL ORDER BY t.id DESC LIMIT 1",(vid,)).fetchone()
            if previous:c.execute('UPDATE discovery_works SET last_checked_at=? WHERE video_id=?',(previous[0],vid))
        if vid in paused:c.execute('UPDATE discovery_works SET enabled=0 WHERE video_id=?',(vid,))
        if relevant:
            import asset_references
            asset_references.save_content(c,row)
            # New discovery must not rewrite user-selected priority or enable an existing paused target.
            c.execute('''INSERT INTO videos(source_id,external_id,title,url,game,created_at) VALUES(?,?,?,?,?,?)
              ON CONFLICT(source_id,external_id) DO NOTHING''',(source_row[0],vid,title[:300],f'https://www.douyin.com/video/{vid}',app.TARGET_GAME,stamp))
            pk=c.execute('SELECT id FROM videos WHERE source_id=? AND external_id=?',(source_row[0],vid)).fetchone()[0]
            if row.get('metrics'):video_metadata.save(c,pk,row['metrics'])
            import asset_verticality
            asset_verticality.refresh(c,'work',vid)


def select_work_targets(c,plan,instant,focused,paused):
    """Prioritize vertical assets; retain ordinary monitoring and fair exploration."""
    blocked_sql=','.join('?' for _ in paused) or "''"
    focused_sql=','.join('?' for _ in focused) or "''"
    limit=plan['video_limit']
    turn=c.execute("SELECT COUNT(*) FROM discovery_jobs WHERE kind='work' AND settled=1").fetchone()[0]
    # Only return at most limit rows per group, even for a large durable library.
    rows=c.execute(f'''WITH activity AS ({RECENT_ACTIVITY}), eligible AS (
      SELECT DISTINCT w.*,activity.latest_comment_at,
        COALESCE(json_extract(a.result,'$.matched'),0) AS vertical,CASE
        WHEN activity.latest_comment_at IS NOT NULL THEN 'active'
        WHEN w.last_checked_at IS NULL AND w.author_sec_uid IN ({focused_sql}) THEN 'focused_new'
        ELSE 'rotation' END AS selection_group
      FROM discovery_works w JOIN videos v ON v.external_id=w.video_id
      LEFT JOIN asset_verticality a ON a.kind='work' AND a.asset_key=w.video_id
      LEFT JOIN activity ON activity.page_url='https://www.douyin.com/video/'||w.video_id
      WHERE w.relevant=1 AND w.enabled=1 AND v.enabled=1 AND (w.author_sec_uid IS NULL OR NOT EXISTS
        (SELECT 1 FROM discovery_authors a WHERE a.sec_uid=w.author_sec_uid AND a.enabled=0))
      AND w.video_id NOT IN ({blocked_sql})
      AND (w.next_check_at IS NULL OR julianday(w.next_check_at)<=julianday(?))
    ), ranked AS (
      SELECT *,ROW_NUMBER() OVER(PARTITION BY vertical,selection_group ORDER BY
        CASE WHEN vertical=1 AND last_checked_at IS NULL AND ?%3!=2
          THEN julianday(published_at) END DESC,
        julianday(COALESCE(next_check_at,first_seen_at)),julianday(published_at) DESC,video_id) AS selection_rank
      FROM eligible
    ) SELECT * FROM ranked WHERE selection_rank<=? ORDER BY selection_group,selection_rank''',
      (instant,instant,instant,*focused,*paused,instant,turn,limit)).fetchall()
    def select_pool(pool,budget):
        queues={name:deque(r for r in pool if r['selection_group']==name) for name in ('active','focused_new','rotation')}
        selected=[]
        def take(name):
            if queues[name]:selected.append(queues[name].popleft());return True
            return False
        if budget==1:
            cycle=('active','active','focused_new','rotation')
            for offset in range(len(cycle)):
                if take(cycle[(turn+offset)%len(cycle)]):break
        else:
            reserved=int(bool(queues['focused_new'] or queues['rotation']))
            while len(selected)<budget-reserved and take('active'):pass
            exploration=0
            while len(selected)<budget and (queues['focused_new'] or queues['rotation']):
                preferred='rotation' if (turn+exploration)%3==2 else 'focused_new'
                if not take(preferred):take('focused_new' if preferred=='rotation' else 'rotation')
                exploration+=1
            while len(selected)<budget and take('active'):pass
        return selected
    vertical=[r for r in rows if r['vertical']]
    ordinary=[r for r in rows if not r['vertical']]
    if vertical and ordinary:
        vertical_budget=int(turn%3!=2) if limit==1 else min(limit-1,(limit*2+2)//3)
        selected=select_pool(vertical,vertical_budget)+select_pool(ordinary,limit-vertical_budget)
        # Unused reservations return to the other pool, within the same request budget.
        selected_ids={r['video_id'] for r in selected}
        remaining=[r for r in vertical+ordinary if r['video_id'] not in selected_ids]
        selected+=select_pool(remaining,limit-len(selected))
    else:
        selected=select_pool(vertical or ordinary,limit)
    audit=dict(version='work-vertical-priority-v3',turn=turn,activity_window_seconds=3600,
               slots=[dict(video_id=r['video_id'],group=r['selection_group'],
                           vertical=bool(r['vertical']),
                           latest_comment_at=r['latest_comment_at']) for r in selected])
    return selected,audit


def choose(c,plan,instant):
    """Return (enabled, job). Weighted rotation prevents either layer starving."""
    cfg=config(c)
    if not cfg['enabled'] or not plan['continuous']:return False,None
    # A permitted retry stays on the frozen failed job. It never switches entry
    # points to turn an unresolved challenge into apparently successful discovery.
    if dict(plan).get('last_task_id'):
        previous=c.execute('SELECT status FROM collection_tasks WHERE id=?',(plan['last_task_id'],)).fetchone()
        frozen=worker_config(c,plan['last_task_id'])
        if previous and previous['status']!='completed' and frozen:return True,frozen
    authors=author_rows(c,cfg)
    due=lambda stamp:not stamp or datetime.fromisoformat(stamp)<=datetime.fromisoformat(instant)
    jobs={}
    candidates=[r for r in authors if r['enabled'] and due(r['next_check_at'])]
    if candidates:
        author_turn=c.execute("SELECT COUNT(*) FROM discovery_jobs WHERE kind='author' AND settled=1").fetchone()[0]
        focused_due=[r for r in candidates if r['focused']]
        ordinary_due=[r for r in candidates if not r['focused']]
        preferred=ordinary_due if author_turn%3==2 else focused_due
        a=min(preferred or candidates,key=lambda r:(r['next_check_at'] or r['first_seen_at'],r['sec_uid']))
        jobs['author']=dict(kind='author',target=a['seed_video_id'],transport='http',key=a['sec_uid'],channel='author',
            author_selection=dict(version='vertical-author-priority-v1',turn=author_turn,focused=bool(a['focused'])),
            author_pages=cfg['initial_author_pages'] if not a['last_checked_at'] else 1)
    else:
        for seed in cfg['seed_videos']:
            if not c.execute('SELECT 1 FROM discovery_works WHERE video_id=? AND author_sec_uid IS NOT NULL',(seed,)).fetchone():
                prior=c.execute("SELECT t.finished_at FROM discovery_jobs j JOIN collection_tasks t ON t.id=j.task_id WHERE j.key=? AND t.finished_at IS NOT NULL ORDER BY j.task_id DESC LIMIT 1",('seed:'+seed,)).fetchone()
                if prior and not due(future(prior[0],cfg['author_interval'])):continue
                jobs['author']=dict(kind='author',target=seed,transport='http',key='seed:'+seed,channel='author',author_pages=cfg['initial_author_pages']);break
    import asset_references
    _,reference_queries=asset_references.discovery_assets(c)
    search_terms=set(cfg['keywords'])|set(reference_queries)
    query=next((dict(r) for r in c.execute('SELECT * FROM discovery_queries ORDER BY COALESCE(last_checked_at,\'\'),keyword') if r['keyword'] in search_terms and due(r['next_check_at'])),None)
    if query:jobs['search']=dict(kind='search',target=query['keyword'],transport=plan['transport'] if plan['kind']=='search' else 'local_browser',key=query['keyword'],channel='search')
    paused=paused_targets(c)
    focused={r['sec_uid'] for r in authors if r['focused'] and r['enabled']}
    rows,selection=select_work_targets(c,plan,instant,focused,paused)
    if rows:jobs['work']=dict(kind='video',target='\n'.join(r['video_id'] for r in rows),transport='http',key='work',channel='work',work_selection=selection)
    rotation=['work','author','work','search'];start=plan['run_count']%len(rotation)
    chosen=next((jobs[rotation[(start+i)%len(rotation)]] for i in range(len(rotation)) if rotation[(start+i)%len(rotation)] in jobs),None)
    return True,{**chosen,'policy':cfg} if chosen else None


def attach(c,task_id,job):
    if job:
        c.execute('INSERT INTO discovery_jobs(task_id,kind,key,config) VALUES(?,?,?,?)',
          (task_id,job['channel'],job['key'],json.dumps(job,ensure_ascii=False)))


def worker_config(c,task_id):
    row=c.execute('SELECT config FROM discovery_jobs WHERE task_id=?',(task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def settle(c,task):
    job=c.execute('SELECT * FROM discovery_jobs WHERE task_id=? AND settled=0',(task['id'],)).fetchone()
    if not job or not task['finished_at']:return
    frozen=json.loads(job['config']);cfg=frozen['policy'];stamp=task['finished_at']
    if task['status']=='completed':
        if job['kind']=='search':
            c.execute('UPDATE discovery_queries SET last_checked_at=?,next_check_at=? WHERE keyword=?',(stamp,future(stamp,cfg['search_interval']),job['key']))
        if job['kind']=='author':
            authors=author_rows(c,cfg)
            seed=frozen['target']
            matched=c.execute('SELECT author_sec_uid FROM discovery_works WHERE video_id=?',(seed,)).fetchone()
            sec=matched[0] if matched else job['key']
            a=next((r for r in authors if r['sec_uid']==sec),None)
            if a:c.execute('UPDATE discovery_authors SET last_checked_at=?,next_check_at=? WHERE sec_uid=?',
                (stamp,future(stamp,cfg['focus_interval'] if a['focused'] else cfg['author_interval']),sec))
    # Even in a partial batch, completed work checkpoints remain verifiable.
    recent=dict(c.execute(RECENT_ACTIVITY,(stamp,stamp,stamp)).fetchall())
    for cp in c.execute("SELECT video_id FROM collection_checkpoints WHERE task_id=? AND status='done'",(task['id'],)).fetchall():
        vid=cp[0];row=c.execute('SELECT quiet_streak FROM discovery_works WHERE video_id=?',(vid,)).fetchone()
        if not row:continue
        new=c.execute('''SELECT COUNT(*) FROM collection_observations o WHERE task_id=? AND kind='comment' AND page_url=?
          AND julianday(published_at)>=julianday(?,'-1 hour') AND julianday(published_at)<=julianday(observed_at)
          AND NOT EXISTS(SELECT 1 FROM collection_observations p WHERE p.kind='comment' AND p.page_url=o.page_url AND p.external_id=o.external_id AND p.task_id<o.task_id)''',
          (task['id'],f'https://www.douyin.com/video/{vid}',task['created_at'])).fetchone()[0]
        quiet=0 if new else min(row[0]+1,5)
        # A quiet read is still recorded, but cannot immediately demote work
        # with genuinely recent comments. Re-reading old text never extends it.
        interval=cfg['work_interval']*(1 if f'https://www.douyin.com/video/{vid}' in recent else 2**quiet)
        c.execute('UPDATE discovery_works SET last_checked_at=?,next_check_at=?,quiet_streak=?,new_recent_comments=new_recent_comments+? WHERE video_id=?',
            (stamp,future(stamp,interval),quiet,new,vid))
    c.execute('UPDATE discovery_jobs SET settled=1 WHERE task_id=?',(task['id'],))
