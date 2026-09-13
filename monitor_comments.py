"""Paginated local comment history. Reads never collect or dispatch analysis."""
import clubops as app
import comment_filters
import monitoring
import semantic


# Group once, then use indexed task identities to prefer an accepted matching snapshot.
# The padded Julian-day prefix preserves chronological ordering across UTC offsets;
# the suffix preserves the original timestamp and tie order. MATERIALIZED prevents
# SQLite from re-running the correlated preference lookup through the outer joins.
HISTORY = """WITH observed_groups AS (
 SELECT page_url,external_id,MAX(task_id) AS latest_task_id,
 substr(MIN(printf('%030.17f',julianday(observed_at)) || observed_at),31) AS earliest_observed_at
 FROM collection_observations
 WHERE kind='comment' AND trim(comment_text)!='' AND (:video='' OR page_url=:video)
 GROUP BY page_url,external_id
), observed_latest AS (
 SELECT o.page_url,o.external_id,o.task_id,o.comment_text,o.user_identifier,g.earliest_observed_at FROM observed_groups g
 JOIN collection_observations o ON o.kind='comment' AND o.task_id=g.latest_task_id
 AND o.external_id=g.external_id AND o.page_url=g.page_url
), preferred AS MATERIALIZED (
 SELECT l.*,COALESCE((SELECT MAX(p.task_id) FROM collection_observations p
 WHERE p.kind='comment' AND p.page_url=l.page_url AND p.external_id=l.external_id
 AND p.filter_reason='' AND p.comment_text=l.comment_text
 AND COALESCE(p.user_identifier,'')=COALESCE(l.user_identifier,'')),l.task_id) AS chosen_task_id
 FROM observed_latest l
), archive_fallback AS (
 SELECT x.*,v.url,v.title,p.external_id AS user_identifier,
 ROW_NUMBER() OVER(PARTITION BY v.url,x.external_id ORDER BY x.discovered_at DESC) AS archive_rank
 FROM comments x JOIN videos v ON v.id=x.video_id LEFT JOIN people p ON p.id=x.person_id
 WHERE trim(x.raw_text)!='' AND (:video='' OR v.url=:video) AND NOT EXISTS (
 SELECT 1 FROM collection_observations o WHERE o.kind='comment' AND o.external_id=x.external_id
 AND o.page_url=v.url AND trim(o.comment_text)!='')
), scoped AS (
 SELECT o.external_id,o.page_url AS video_url,o.comment_text AS text,o.nickname,o.user_identifier,
 o.published_at,o.observed_at,o.filter_reason,o.task_id,o.ingest_disposition,t.include_keywords,t.exclude_keywords,
 'observation' AS text_origin,x.id AS comment_id,x.discovered_at AS first_seen_at,v.title AS video_title,
 l.comment_text AS latest_text,l.user_identifier AS latest_user,1 AS rank,
 COALESCE(x.discovered_at,l.earliest_observed_at) AS collected_at
 FROM preferred l JOIN collection_observations o ON o.task_id=l.chosen_task_id AND o.kind='comment'
 AND o.external_id=l.external_id AND o.page_url=l.page_url
 JOIN collection_tasks t ON t.id=o.task_id
 LEFT JOIN comments x ON x.source_id=:source AND x.external_id=o.external_id
 LEFT JOIN videos v ON v.source_id=:source AND v.url=o.page_url
 UNION ALL
 SELECT external_id,url,raw_text,observed_nickname,user_identifier,published_at,discovered_at,
 '',NULL,'','','','archive',id,discovered_at,title,raw_text,user_identifier,1,discovered_at
 FROM archive_fallback WHERE archive_rank=1
)
"""


def history(query, mode='live'):
    page = monitoring.integer(query.get('page', '1'), '页码', 1, 1000000)
    video = query.get('video', '')
    search = query.get('q', '')
    state = query.get('filter', 'all')
    if not isinstance(video, str) or len(video)>300 or not isinstance(search, str) or len(search)>200 or state not in ('all','accepted','filtered','valuable'):
        raise ValueError('评论筛选参数无效')
    engine = semantic.state()['engine'] if mode=='live' else None
    with app.db(mode) as c:
        # Sort only identity/text fields, then attach display metadata to the
        # selected rows. A WAL snapshot keeps counters and analysis consistent
        # without blocking collectors on the application's writer lock.
        c.execute('PRAGMA temp_store=MEMORY')
        c.execute('BEGIN')
        source = c.execute("SELECT id FROM sources WHERE kind='browser' ORDER BY id LIMIT 1").fetchone()
        params = dict(source=source[0] if source else -1,video=video,query=search,state=state)
        # Rank the history once per response, not again for every counter, filter
        # and page. This temporary table belongs to this connection only and does
        # not modify the archive or cache results across analysis/config changes.
        c.execute('CREATE TEMP TABLE monitor_comment_scope AS '+HISTORY+'SELECT * FROM scoped',params)
        counts = c.execute("SELECT COUNT(*) AS observed,COALESCE(SUM(filter_reason=''),0) AS accepted,COALESCE(SUM(filter_reason!=''),0) AS filtered FROM monitor_comment_scope").fetchone()
        # Use the same current, input-bound projection as the visible judgment.
        # Evaluate eligibility before pagination; manual corrections take priority.
        current, valuable = {}, set()
        for accepted in c.execute("SELECT * FROM monitor_comment_scope WHERE filter_reason=''"):
            key = (accepted['video_url'],accepted['external_id'])
            current[key] = monitoring.observation_analysis(c,accepted['comment_id'],accepted['text'],engine,summary=True)
            if current[key].get('category') == 'buyer' and current[key].get('analysis_method') in ('model','human'):
                valuable.add(key)
        where = " WHERE (:state='all' OR (:state IN ('accepted','valuable') AND filter_reason='') OR (:state='filtered' AND filter_reason!='')) AND (:query='' OR instr(lower(text || ' ' || nickname || ' ' || COALESCE(user_identifier,'') || ' ' || external_id),lower(:query))>0)"
        total = c.execute('SELECT COUNT(*) FROM monitor_comment_scope'+where,params).fetchone()[0]
        ordered = 'SELECT * FROM monitor_comment_scope'+where+' ORDER BY julianday(collected_at) DESC,video_url,external_id'
        valuable_source = None
        if state == 'valuable':
            valuable_source = [r for r in c.execute(ordered,params) if (r['video_url'],r['external_id']) in valuable]
            total = len(valuable_source)
        pages = max(1,(total+24)//25)
        page = min(page,pages)
        params['offset'] = (page-1)*25
        source_rows = valuable_source[params['offset']:params['offset']+25] if valuable_source is not None else c.execute(ordered+' LIMIT 25 OFFSET :offset',params).fetchall()
        target = monitoring.freshness_target(c)
        rows = []
        for source_row in source_rows:
            row = dict(source_row)
            row.pop('rank');row.pop('latest_text');row.pop('latest_user')
            row.update(monitoring.observation_analysis(c,row['comment_id'],row['text'],engine) if not row['filter_reason'] else dict(category=None,analysis_method=None,analysis_state=None))
            row['include_matches'] = comment_filters.matches(row['text'],row.pop('include_keywords'))
            row['exclude_matches'] = comment_filters.matches(row['text'],row.pop('exclude_keywords'))
            row['timing'] = monitoring.timing(row,target)
            rows.append(row)
        return dict(rows=rows,counts=dict(counts),valuable_count=len(valuable),total=total,page=page,pages=pages,page_size=25,
                    video_url=video,scope='all_local_comment_history',sort='collected_at_desc',target_seconds=target)
