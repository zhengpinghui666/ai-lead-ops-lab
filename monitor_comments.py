"""Paginated local comment history. Reads never collect or dispatch analysis."""
import clubops as app
import comment_filters
import monitoring
import semantic


HISTORY = """WITH raw AS (
 SELECT o.external_id,o.page_url AS video_url,o.comment_text AS text,o.nickname,
 o.user_identifier,o.published_at,o.observed_at,o.filter_reason,o.task_id,
 o.ingest_disposition,t.include_keywords,t.exclude_keywords,'observation' AS text_origin,
 x.id AS comment_id,x.discovered_at AS first_seen_at,v.title AS video_title
 FROM collection_observations o JOIN collection_tasks t ON t.id=o.task_id
 LEFT JOIN comments x ON x.source_id=:source AND x.external_id=o.external_id
 LEFT JOIN videos v ON v.source_id=:source AND v.url=o.page_url
 WHERE o.kind='comment' AND trim(o.comment_text)!='' AND (:video='' OR o.page_url=:video)
 UNION ALL
 SELECT x.external_id,v.url,x.raw_text,x.observed_nickname,p.external_id,x.published_at,
 x.discovered_at,'',NULL,'','','','archive',x.id,x.discovered_at,v.title
 FROM comments x JOIN videos v ON v.id=x.video_id LEFT JOIN people p ON p.id=x.person_id
 WHERE trim(x.raw_text)!='' AND (:video='' OR v.url=:video) AND NOT EXISTS (
  SELECT 1 FROM collection_observations o WHERE o.kind='comment'
  AND o.external_id=x.external_id AND o.page_url=v.url AND trim(o.comment_text)!='')
), latest AS (
 SELECT *,FIRST_VALUE(text) OVER(PARTITION BY video_url,external_id ORDER BY task_id DESC,observed_at DESC) AS latest_text,
 FIRST_VALUE(user_identifier) OVER(PARTITION BY video_url,external_id ORDER BY task_id DESC,observed_at DESC) AS latest_user
 FROM raw
), ranked AS (
 SELECT *,ROW_NUMBER() OVER(PARTITION BY video_url,external_id ORDER BY
 CASE WHEN filter_reason='' AND text=latest_text AND COALESCE(user_identifier,'')=COALESCE(latest_user,'') THEN 0 ELSE 1 END,
 task_id DESC,observed_at DESC) AS rank,
 COALESCE(first_seen_at,FIRST_VALUE(observed_at) OVER(PARTITION BY video_url,external_id ORDER BY julianday(observed_at),observed_at)) AS collected_at FROM latest
), scoped AS (SELECT * FROM ranked WHERE rank=1 AND (:video='' OR video_url=:video))
"""


def history(query, mode='live'):
    page = monitoring.integer(query.get('page', '1'), '页码', 1, 1000000)
    video = query.get('video', '')
    search = query.get('q', '')
    state = query.get('filter', 'all')
    if not isinstance(video, str) or len(video)>300 or not isinstance(search, str) or len(search)>200 or state not in ('all','accepted','filtered','valuable'):
        raise ValueError('评论筛选参数无效')
    engine = semantic.state()['engine'] if mode=='live' else None
    with app.LOCKS[mode], app.db(mode) as c:
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
            current[key] = monitoring.observation_analysis(c,accepted['comment_id'],accepted['text'],engine)
            if current[key].get('category') == 'buyer' and current[key].get('analysis_method') in ('rules','model','human'):
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
            row.update(current[(row['video_url'],row['external_id'])] if not row['filter_reason'] else dict(category=None,analysis_method=None,analysis_state=None))
            row['include_matches'] = comment_filters.matches(row['text'],row.pop('include_keywords'))
            row['exclude_matches'] = comment_filters.matches(row['text'],row.pop('exclude_keywords'))
            row['timing'] = monitoring.timing(row,target)
            rows.append(row)
        return dict(rows=rows,counts=dict(counts),valuable_count=len(valuable),total=total,page=page,pages=pages,page_size=25,
                    video_url=video,scope='all_local_comment_history',sort='collected_at_desc',target_seconds=target)
