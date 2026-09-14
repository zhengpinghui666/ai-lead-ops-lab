"""Observed service relevance and publication activity, never customer intent.

One row per platform work/comment identity prevents repeated pages or account
rotation from inflating weights. Missing timestamps never become recent activity.
"""
import json
import re
from datetime import datetime, timezone

VERSION = 'work-cadence-v1'
POLICY_VERSION = 'work-cadence-v2-quiet-decay'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS work_comment_signals (
 video_id TEXT NOT NULL, external_id TEXT NOT NULL,
 published_at TEXT, published_ts REAL, first_observed_at TEXT NOT NULL,
 observed_ts REAL NOT NULL, related INTEGER NOT NULL,
 PRIMARY KEY(video_id,external_id)
);
CREATE INDEX IF NOT EXISTS idx_work_signal_publication ON work_comment_signals(published_ts,video_id);
CREATE TABLE IF NOT EXISTS work_cadence_migrations (version TEXT PRIMARY KEY,rows_count INTEGER NOT NULL);
'''
SERVICE = re.compile(r'陪玩|陪陪|陪练|陪打|陪排|带练|代练|代打|男陪|女陪|技术陪|娱乐陪|点陪|陪\s*[wW]|'
                     r'打手|板板|点单|下单|接单|等单|招募|俱乐部|老板点|找老板|求老板|蹲老板|'
                     r'付费教学|有偿教学|复盘接单', re.I)


def epoch(value):
    if not isinstance(value,str) or not value.strip():return None
    try:
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        return d.replace(tzinfo=timezone.utc).timestamp() if d.tzinfo is None else d.timestamp()
    except (ValueError,OverflowError):return None


def record(c,video_id,external_id,text,published_at,observed_at,*,first_observed_at=None):
    """No raw text is copied into the signal cache; immutable archive is the source."""
    observed=epoch(observed_at)
    if not re.fullmatch(r'\d{5,30}',str(video_id)) or not external_id or not (text or '').strip() or observed is None:return False
    published=epoch(published_at)
    if published is not None and (published<0 or published>observed):published=published_at=None
    related=int(bool(SERVICE.search(text)))
    c.execute('''INSERT INTO work_comment_signals VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(video_id,external_id) DO UPDATE SET
      published_at=COALESCE(excluded.published_at,work_comment_signals.published_at),
      published_ts=COALESCE(excluded.published_ts,work_comment_signals.published_ts),
      observed_ts=excluded.observed_ts,related=excluded.related
      WHERE excluded.observed_ts>=work_comment_signals.observed_ts''',
      (str(video_id),str(external_id),published_at if published is not None else None,published,
       first_observed_at or observed_at,observed,related))
    return True


def initialize(c):
    """Idempotent local backfill; never creates observations, models or messages."""
    if c.execute('SELECT 1 FROM work_cadence_migrations WHERE version=?',(VERSION,)).fetchone():return
    rows=c.execute("""WITH latest AS (
       SELECT page_url,external_id,MAX(task_id) task_id,MIN(julianday(observed_at)) first_jd
       FROM collection_observations WHERE kind='comment' AND trim(comment_text)!=''
       GROUP BY page_url,external_id)
       SELECT o.page_url,o.external_id,o.comment_text,o.published_at,o.observed_at,
       strftime('%Y-%m-%dT%H:%M:%S+00:00',l.first_jd) first_observed_at
       FROM latest l JOIN collection_observations o ON o.task_id=l.task_id
       AND o.kind='comment' AND o.page_url=l.page_url AND o.external_id=l.external_id""").fetchall()
    for row in rows:
        m=re.fullmatch(r'https://www\.douyin\.com/video/(\d{5,30})/?',row['page_url'] or '')
        if m:record(c,m[1],row['external_id'],row['comment_text'],row['published_at'],row['observed_at'],first_observed_at=row['first_observed_at'])
    count=c.execute('SELECT COUNT(*) FROM work_comment_signals').fetchone()[0]
    c.execute('INSERT INTO work_cadence_migrations VALUES(?,?)',(VERSION,count))


def statistics(c,instant):
    now=epoch(instant)
    if now is None:raise ValueError('Invalid cadence clock')
    rows=c.execute('''SELECT video_id,COUNT(*) total_samples,SUM(related) total_hits,
      SUM(published_ts>=? AND published_ts<=?) samples,
      SUM(related=1 AND published_ts>=? AND published_ts<=?) hits,
      SUM(published_ts>=? AND published_ts<=?) recent_five_minutes,
      SUM(published_ts>=? AND published_ts<=?) recent_hour,
      SUM(published_ts>=? AND published_ts<?) previous_hour,
      MAX(CASE WHEN published_ts<=? THEN published_ts END) latest_publication
      FROM work_comment_signals WHERE observed_ts<=? GROUP BY video_id''',
      (now-604800,now,now-604800,now,now-300,now,now-3600,now,now-7200,now-3600,now,now))
    return {r['video_id']:{key:r[key] or 0 for key in r.keys() if key!='video_id'} for r in rows}


def assess(*,vertical,quiet,base_interval,stats):
    """Conservative levels, decaying burst boost and explicit sample coverage.

    Ratios are observed keyword relevance, not a full-comment or buyer ratio.
    Sparse evidence cannot put a work into the monthly tier.
    """
    n=stats.get('samples',0);hits=stats.get('hits',0);basis='published_last_7_days'
    if n<30 and stats.get('total_samples',0)>=50:
        n=stats['total_samples'];hits=stats['total_hits'];basis='observed_history'
    confidence=n>=30 if basis=='published_last_7_days' else n>=50
    ratio=hits/n if n else None
    quiet=max(0,min(int(quiet),5));base=max(30,int(base_interval))
    if not confidence:
        tier='explore';score=45 if vertical else 15
        interval=min(3600 if vertical else 86400,(base if vertical else max(base,3600))*2**quiet)
    elif ratio>=.25:tier,score,interval='high',90,base
    elif ratio>=.10:tier,score,interval='relevant',70,base*3
    elif ratio>=.03:tier,score,interval='potential',50,base*15
    elif ratio>=.02 or n<100:tier,score,interval='low',20,21600
    else:tier,score,interval='monthly',5,2592000
    # A service-specific title is independent evidence, so a thin or noisy
    # observed sample cannot silently remove its monitoring for a month.
    if vertical and interval>3600:tier,score,interval='title_watch',max(score,40),3600
    activity=stats.get('recent_hour',0);five=stats.get('recent_five_minutes',0)
    previous=stats.get('previous_hour',0)
    related=vertical or confidence and ratio>=.03
    burst=bool(related and (five>=5 or activity>=20 and activity>=max(3,previous)*3))
    boost='none'
    if burst:interval=min(interval,max(30,base//2));boost='burst';score=max(score,95)
    elif related and activity>=3:interval=min(interval,base);boost='active';score=max(score,80)
    elif vertical and not confidence and activity and quiet<2:interval=min(interval,base)
    return dict(version=POLICY_VERSION,tier=tier,score=score,interval_seconds=min(2592000,int(interval)),
                ratio=round(ratio,4) if ratio is not None else None,sample_count=n,service_hits=hits,
                sample_basis=basis,sufficient_samples=confidence,
                recent_five_minutes=five,recent_hour=activity,previous_hour=previous,boost=boost,
                quiet_priority_expired=bool(activity and boost=='none' and quiet>=2),
                complete_comment_coverage=False)
