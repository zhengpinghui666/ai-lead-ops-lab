"""Read-only freshness evidence for continuous collection; no platform/model calls."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time


def instant(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except ValueError:
        return None


def seconds(start, end):
    a, b = instant(start), instant(end)
    return round((b-a).total_seconds(), 3) if a and b and b >= a else None


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return dict(n=0, p50_seconds=None, p95_seconds=None, max_seconds=None,
                    within_60_seconds=None)
    return dict(n=len(values), p50_seconds=values[math.ceil(len(values)*.5)-1],
                p95_seconds=values[math.ceil(len(values)*.95)-1], max_seconds=values[-1],
                within_60_seconds=sum(v <= 60 for v in values))


def audit(c, since, until):
    """Use first inserted observations, not repeated reads or current model counts.

    Legacy observations without ingest dispositions cannot prove first insertion.
    The collector owns one browser source; ambiguous source identity fails closed.
    Asset verticality is reported as of this snapshot, not reconstructed history.
    """
    start, end = instant(since), instant(until)
    if not start or not end or start >= end:
        raise ValueError('需要带时区的有效起止时间，且开始早于结束')
    sources = list(c.execute("SELECT id FROM sources WHERE kind='browser'"))
    if len(sources) != 1:
        raise ValueError('采集来源不唯一，无法可靠关联首次入库证据')
    source = sources[0][0]
    plans = {r[0] for r in c.execute('SELECT id FROM collection_plans WHERE continuous=1')}
    tasks = {r['id']: dict(r) for r in c.execute('SELECT id,request_id,status,created_at,finished_at,lookback_hours FROM collection_tasks')}
    def continuous(task):
        match = re.fullmatch(r'plan-([1-9][0-9]*)-run-([1-9][0-9]*)', task['request_id'])
        return bool(match and int(match[1]) in plans)
    observed = list(c.execute('''SELECT o.*,v.id AS video_pk,w.first_seen_at AS work_first_seen_at,
            COALESCE(json_extract(a.result,'$.matched'),0) AS vertical
        FROM collection_observations o JOIN videos v ON o.page_url='https://www.douyin.com/video/'||v.external_id AND v.source_id=?
        LEFT JOIN discovery_works w ON w.video_id=v.external_id
        LEFT JOIN asset_verticality a ON a.kind='work' AND a.asset_key=v.external_id
        WHERE o.kind='comment' AND julianday(o.observed_at)>=julianday(?)
        AND julianday(o.observed_at)<=julianday(?) ORDER BY julianday(o.observed_at),o.task_id''',
        (source, start.isoformat(), end.isoformat())))
    excluded = Counter()
    samples = []
    for o in observed:
        if o['filter_reason'] or o['ingest_disposition'] != 'inserted':
            continue
        task = tasks[o['task_id']]
        if not continuous(task):
            excluded['manual_or_other_plan_insertions'] += 1
            continue
        comment = c.execute('''SELECT id,discovered_at FROM comments WHERE source_id=?
            AND video_id=? AND external_id=?''', (source, o['video_pk'], o['external_id'])).fetchone()
        if not comment or instant(comment['discovered_at']) != instant(o['observed_at']):
            excluded['first_insertion_not_matched'] += 1
            continue
        lag = seconds(o['published_at'], o['observed_at'])
        if lag is None:
            excluded['missing_invalid_or_future_publication'] += 1
            continue
        if not task['lookback_hours'] or lag > task['lookback_hours']*3600:
            excluded['outside_recorded_freshness_window'] += 1
            continue
        # A previous read of this work, including filtered comments, distinguishes
        # first coverage from monitoring. A previous failed partial read also counts.
        prior = c.execute('''SELECT MAX(julianday(observed_at)) AS latest_jd,
            COUNT(DISTINCT CASE WHEN julianday(observed_at)>=julianday(?) THEN task_id END) AS batches_after_publication
            FROM collection_observations WHERE kind='comment'
            AND page_url=? AND task_id!=? AND julianday(observed_at)<julianday(?)''',
            (o['published_at'],o['page_url'],o['task_id'],o['observed_at'])).fetchone()
        previously_observed = prior['latest_jd'] is not None
        jobs = [dict(r) for r in c.execute('''SELECT id,status,created_at,started_at,finished_at
            FROM semantic_jobs WHERE evidence_type='comment' AND record_id=?
            AND julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?)
            ORDER BY julianday(created_at),id''', (comment['id'], o['observed_at'], end.isoformat()))]
        job = jobs[0] if jobs else None
        # A running job may have finished after the requested audit cutoff.
        complete = bool(job and job['status']=='completed' and instant(job['finished_at'])
                        and instant(job['finished_at']) <= end)
        samples.append(dict(comment_id=comment['id'], task_id=task['id'],
            published_at=o['published_at'], first_collected_at=o['observed_at'],
            cohort='previously_observed_work' if previously_observed else 'first_work_coverage',
            vertical_at_snapshot=bool(o['vertical']), collection_seconds=lag,
            work_wait_seconds=seconds(o['work_first_seen_at'],o['observed_at']) if not previously_observed else None,
            earlier_read_batches_after_publication=prior['batches_after_publication'],
            first_model_job_id=job['id'] if job else None,
            model_status_at_snapshot=job['status'] if job else 'not_queued',
            queue_wait_seconds=seconds(job['created_at'],job['started_at']) if job and instant(job['started_at'])
                and instant(job['started_at'])<=end else None,
            model_seconds=seconds(job['started_at'],job['finished_at']) if complete else None,
            collect_to_model_seconds=seconds(o['observed_at'],job['finished_at']) if complete else None,
            publish_to_model_seconds=seconds(o['published_at'],job['finished_at']) if complete else None))
    cohorts = {}
    for name in ('all', 'first_work_coverage', 'previously_observed_work'):
        rows = samples if name=='all' else [r for r in samples if r['cohort']==name]
        cohorts[name] = {key: distribution(r[key] for r in rows) for key in
            ('collection_seconds','work_wait_seconds','queue_wait_seconds','model_seconds',
             'collect_to_model_seconds','publish_to_model_seconds')}
    work_pool = [dict(r) for r in c.execute('''SELECT
        COALESCE(json_extract(a.result,'$.matched'),0) AS vertical_at_snapshot,
        COUNT(*) AS enabled_works,SUM(w.last_checked_at IS NULL) AS without_completed_monitor_check,
        SUM(w.last_checked_at IS NULL AND julianday(w.first_seen_at)<julianday(?,'-1 hour')) AS waiting_over_hour
        FROM discovery_works w JOIN videos v ON v.external_id=w.video_id AND v.source_id=?
        LEFT JOIN asset_verticality a ON a.kind='work' AND a.asset_key=w.video_id
        WHERE w.enabled=1 AND w.relevant=1 AND v.enabled=1 AND (w.author_sec_uid IS NULL OR NOT EXISTS
            (SELECT 1 FROM discovery_authors d WHERE d.sec_uid=w.author_sec_uid AND d.enabled=0))
        GROUP BY 1''', (end.isoformat(),source))]
    scheduled_observed = [o for o in observed if continuous(tasks[o['task_id']])]
    return dict(version='continuous-freshness-audit-v1', since=start.isoformat(), until=end.isoformat(),
        generated_at=datetime.now(timezone.utc).isoformat(),
        observation_counts=dict(total=len(observed), continuous=len(scheduled_observed),
            continuous_unique_comments=len({(o['page_url'],o['external_id']) for o in scheduled_observed}),
            continuous_accepted_unique=len({(o['page_url'],o['external_id']) for o in scheduled_observed if not o['filter_reason']})),
        excluded_insertions=dict(excluded), eligible_first_insertions=len(samples),
        cohorts=cohorts, work_pool_at_snapshot=work_pool, samples=samples,
        samples_with_earlier_reads_after_publication=sum(r['earlier_read_batches_after_publication']>0 for r in samples),
        limits=['No unobserved platform comments: not coverage or recall.',
                'Only first inserted observations from currently continuous plans; legacy missing dispositions excluded.',
                'Verticality and model status are current snapshot values, not historical accuracy labels.',
                'First model attempt timing only; no claim of correct intent, delivery or conversion.',
                'Earlier reads after publication do not prove that the platform had exposed that comment on those pages.',
                'Work pool uses completed monitor-check bookkeeping, distinct from any partial/manual observation.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path(__file__).parent/'data/clubops-live.db')
    parser.add_argument('--since', help='ISO timestamp including timezone; defaults to 24 hours ago')
    parser.add_argument('--until', help='ISO timestamp including timezone; defaults to now')
    parser.add_argument('--output', type=Path, help='Optional local JSON report; never writes the database')
    args = parser.parse_args()
    end = instant(args.until) if args.until else datetime.now(timezone.utc)
    if not end:
        parser.error('结束时间必须包含时区')
    start = args.since or (end-timedelta(hours=24)).isoformat()
    with closing(sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro', uri=True, timeout=3)) as c:
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA query_only=ON')
        deadline=time.monotonic()+10
        c.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
        c.execute('BEGIN')
        report=audit(c,start,end.isoformat())
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key!='samples'},ensure_ascii=False))


if __name__=='__main__':
    main()
