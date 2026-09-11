"""Read-only work-level view of actual plans, checkpoints and observed comments."""
from datetime import datetime, timedelta
import json

import clubops as app
import video_metadata
from collector import video_targets


def build(tasks, plans, mode='live'):
    instant = app.now()
    cutoff = (datetime.fromisoformat(instant)-timedelta(hours=1)).isoformat()
    active = {t['id']:t for t in tasks if t.get('active')}
    with app.LOCKS[mode], app.db(mode) as c:
        # Aggregate in SQLite; historical comments are not shipped to this view.
        rows = [dict(r) for r in c.execute("""SELECT v.id,v.external_id,v.title,v.url,v.enabled,
            COUNT(x.id) AS archived_comments,
            SUM(CASE WHEN julianday(x.published_at)>=julianday(?) AND julianday(x.published_at)<=julianday(?)
              AND julianday(x.discovered_at)>=julianday(?) THEN 1 ELSE 0 END) AS fresh_comments
            FROM videos v LEFT JOIN comments x ON x.video_id=v.id
            GROUP BY v.id ORDER BY v.id DESC""",(cutoff,instant,cutoff))]
        by_id = {r['external_id']:r for r in rows}
        memberships = {}
        for plan in plans:
            if plan['kind'] != 'video':
                continue  # Search results are not a fixed work monitoring promise.
            try:
                targets = video_targets(plan['target'])
            except ValueError:
                continue
            for target in targets:
                vid = target['video_id']
                if vid not in by_id:
                    row = dict(id=None,external_id=vid,title=target['video_title'],url=target['video_url'],
                        enabled=True,archived_comments=0,fresh_comments=0)
                    rows.append(row)
                    by_id[vid]=row
                memberships.setdefault(vid,[]).append(plan)
        pending = {r['video_id']:r['n'] for r in c.execute("""SELECT x.video_id,COUNT(*) AS n
            FROM semantic_jobs j JOIN comments x ON x.id=j.record_id WHERE j.evidence_type='comment'
            AND j.status IN ('queued','running','cancelling') GROUP BY x.video_id""")}
        # Fixed query count as the durable library grows; never one lookup per work.
        metrics={r['video_id']:{**json.loads(r['payload_json']),'updated_at':r['updated_at']} for r in c.execute('SELECT * FROM video_metadata')}
        latest_by_video={r['video_id']:r for r in c.execute('''WITH ranked AS (
            SELECT k.*,t.status AS task_status,t.finished_at,t.transport,
            ROW_NUMBER() OVER(PARTITION BY k.video_id ORDER BY k.task_id DESC) AS rank
            FROM collection_checkpoints k JOIN collection_tasks t ON t.id=k.task_id)
            SELECT * FROM ranked WHERE rank=1''')}
        observed_by_batch={(r['task_id'],r['page_url']):r for r in c.execute('''SELECT task_id,page_url,COUNT(*) AS n,
            SUM(ingest_disposition='inserted') AS inserted FROM collection_observations
            WHERE kind='comment' GROUP BY task_id,page_url''')}
        current_by_video={k['video_id']:(t,k) for t in active.values() for k in t.get('checkpoints',[]) if k['status']!='done'}
        import discovery_tracking
        cfg=discovery_tracking.config(c)
        pipeline=cfg['enabled'] and any(p['continuous'] and p['status']=='running' for p in plans)
        catalog={r['video_id']:dict(r) for r in c.execute('''SELECT w.*,a.nickname AS author_name,a.enabled AS author_enabled,
            a.priority AS author_priority FROM discovery_works w LEFT JOIN discovery_authors a ON a.sec_uid=w.author_sec_uid''')}
        for row in rows:
            row['metrics'] = metrics.get(row['id'])
            vid = row['external_id']
            bound = memberships.get(vid,[])
            enabled = [p for p in bound if p['status']=='running']
            latest = latest_by_video.get(vid)
            current = current_by_video.get(vid)
            asset=catalog.get(vid,{})
            auto=bool(pipeline and asset.get('relevant') and asset.get('enabled') and row['enabled'] and asset.get('author_enabled')!=0
                and not bound)
            state = ('reading' if current and current[1]['status']=='reading' else 'queued' if current else
                'monitoring' if enabled or auto and asset.get('last_checked_at') else 'pending_read' if auto else 'attention' if any(p['status']=='attention' for p in bound) else
                'paused' if bound else 'history')
            checked = latest and latest['status'] != 'pending'
            observed = observed_by_batch.get((latest['task_id'],row['url'])) if latest else None
            next_dates = [p['next_run_at'] for p in enabled if p.get('next_run_at')]
            if auto and asset.get('next_check_at'):next_dates.append(asset['next_check_at'])
            row.update(state=state,continuous_monitoring=bool(enabled or auto and asset.get('last_checked_at')),
                author_name=asset.get('author_name'),author_sec_uid=asset.get('author_sec_uid'),
                discovered_at=asset.get('first_seen_at'),published_at=asset.get('published_at'),discovery_source=asset.get('source_kind'),
                plan_ids=[p['id'] for p in bound],active_task_id=current[0]['id'] if current else None,
                latest_task_id=latest['task_id'] if latest else None,
                last_checked_at=(latest['finished_at'] or latest['updated_at']) if checked else None,
                last_status=latest['task_status'] if checked else None,
                next_check_at=min(next_dates) if next_dates and not current else None,
                observed_in_last_batch=observed['n'] if observed else 0,
                inserted_in_last_batch=(observed['inserted'] or 0) if observed else 0,
                model_pending=pending.get(row['id'],0),transport=latest['transport'] if latest else None)
    priority={'reading':0,'queued':1,'pending_read':2,'monitoring':3,'attention':4,'paused':5,'history':6}
    rows.sort(key=lambda r:(priority[r['state']],-(r['id'] or 0)))
    return dict(updated_at=instant,rows=rows,scope='configured_and_archived_works',
        summary=dict(tracked=sum(r['continuous_monitoring'] for r in rows),
            reading=sum(r['state']=='reading' for r in rows),works=len(rows),
            fresh_comments=sum(r['fresh_comments'] or 0 for r in rows),
            model_pending=sum(r['model_pending'] for r in rows),
            active_plans=sum(p['status']=='running' for p in plans)))
