"""Read-only work-level view of actual plans, checkpoints and observed comments."""
from datetime import datetime, timedelta

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
        for row in rows:
            row['metrics'] = video_metadata.project(c,row['id']) if row['id'] else None
            vid = row['external_id']
            bound = memberships.get(vid,[])
            enabled = [p for p in bound if p['status']=='running']
            latest = c.execute("""SELECT k.*,t.status AS task_status,t.finished_at,t.transport
                FROM collection_checkpoints k JOIN collection_tasks t ON t.id=k.task_id
                WHERE k.video_id=? ORDER BY k.task_id DESC LIMIT 1""",(vid,)).fetchone()
            current = next(((t,k) for t in active.values() for k in t.get('checkpoints',[])
                if k['video_id']==vid and k['status']!='done'),None)
            state = ('reading' if current and current[1]['status']=='reading' else 'queued' if current else
                'monitoring' if enabled else 'attention' if any(p['status']=='attention' for p in bound) else
                'paused' if bound else 'history')
            checked = latest and latest['status'] != 'pending'
            observed = c.execute("""SELECT COUNT(*) AS n,SUM(ingest_disposition='inserted') AS inserted
                FROM collection_observations WHERE task_id=? AND page_url=? AND kind='comment'""",
                (latest['task_id'],row['url'])).fetchone() if latest else None
            next_dates = [p['next_run_at'] for p in enabled if p.get('next_run_at')]
            row.update(state=state,continuous_monitoring=bool(enabled),
                plan_ids=[p['id'] for p in bound],active_task_id=current[0]['id'] if current else None,
                latest_task_id=latest['task_id'] if latest else None,
                last_checked_at=(latest['finished_at'] or latest['updated_at']) if checked else None,
                last_status=latest['task_status'] if checked else None,
                next_check_at=min(next_dates) if next_dates and not current else None,
                observed_in_last_batch=observed['n'] if observed else 0,
                inserted_in_last_batch=(observed['inserted'] or 0) if observed else 0,
                model_pending=pending.get(row['id'],0),transport=latest['transport'] if latest else None)
    priority={'reading':0,'queued':1,'monitoring':2,'attention':3,'paused':4,'history':5}
    rows.sort(key=lambda r:(priority[r['state']],-(r['id'] or 0)))
    return dict(updated_at=instant,rows=rows,scope='configured_and_archived_works',
        summary=dict(tracked=sum(r['continuous_monitoring'] for r in rows),
            reading=sum(r['state']=='reading' for r in rows),works=len(rows),
            fresh_comments=sum(r['fresh_comments'] or 0 for r in rows),
            model_pending=sum(r['model_pending'] for r in rows),
            active_plans=sum(p['status']=='running' for p in plans)))
