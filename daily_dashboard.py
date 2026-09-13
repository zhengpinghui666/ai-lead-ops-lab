"""Read-only daily aggregates. Event time, not the current size of lead lists.

No customer text, model inputs, credentials or remote calls enter this response.
Captcha submission counters are cumulative within a batch, not per event ID.
"""
from datetime import datetime, timedelta, timezone
import json
import clubops as app

BEIJING = timezone(timedelta(hours=8))


def parsed(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=BEIJING)
    except (ValueError, TypeError):
        return None


def captcha_summary(c, start, end):
    batches = {}
    for row in c.execute("SELECT task_id,created_at,snapshot FROM collection_diagnostics WHERE stage='captcha_workflow' ORDER BY id"):
        stamp = parsed(row['created_at'])
        if not stamp or stamp > end:
            continue
        try:
            v = json.loads(row['snapshot'])['verification']
        except (ValueError, KeyError, TypeError):
            continue
        b = batches.setdefault(row['task_id'], dict(at=stamp, submitted_at=None, passed=False, failed=False, legacy=False))
        b['at'] = min(b['at'], stamp)
        if v.get('submissions') == 1:
            b['submitted_at'] = min(b['submitted_at'] or stamp, stamp)
        if v.get('phase') == 'accepted':
            if v.get('submissions') == 1 and v.get('platform_verdict') == 'passed' and v.get('verdict_source') in ('visible_platform_result', 'platform_response_message'):
                b['passed'] = True  # The producer emits accepted only after a fresh valid data response.
            else:
                b['legacy'] = True
        if v.get('platform_verdict') == 'failed':
            b['failed'] = True

    def summarize(begin):
        encounters = [b for b in batches.values() if begin <= b['at'] <= end]
        submitted = [b for b in batches.values() if b['submitted_at'] and begin <= b['submitted_at'] <= end]
        passed = sum(b['passed'] for b in submitted)
        failed = sum(b['failed'] and not b['passed'] for b in submitted)
        return dict(encounters=len(encounters), submitted=len(submitted), passed=passed,
                    failed=failed, unknown=len(submitted)-passed-failed,
                    not_submitted=sum(b['submitted_at'] is None for b in encounters),
                    legacy_accepted=sum(b['legacy'] and not b['passed'] for b in submitted),
                    pass_rate=round(passed/len(submitted)*100, 1) if submitted else None)
    today = summarize(start)
    today['all_time'] = summarize(datetime.min.replace(tzinfo=timezone.utc))
    events = [(b['submitted_at'], b['passed']) for b in batches.values()
              if b['submitted_at'] and start <= b['submitted_at'] <= end]
    return today, events


def snapshot(mode='live', reference=None):
    end = (reference or datetime.now(timezone.utc)).astimezone(BEIJING)
    start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    boundaries = [start + timedelta(hours=h) for h in range(1, end.hour+1)] + [end]
    series = {}
    totals = {}
    with app.db(mode) as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('PRAGMA temp_store=MEMORY')
        c.execute('BEGIN')  # One WAL read snapshot, without taking the collector's writer lock.
        args = dict(start=start.isoformat(), end=end.isoformat())

        def measure(key, source):
            # Only hour/count pairs leave SQLite. SQL normalizes UTC and +08:00 dates.
            rows = c.execute(f"""SELECT strftime('%H',stamp,'+8 hours') AS hour,COUNT(*) AS n
                FROM ({source}) WHERE julianday(stamp)>=julianday(:start)
                AND julianday(stamp)<=julianday(:end) GROUP BY hour""", args).fetchall()
            hours = {int(r['hour']):r['n'] for r in rows if r['hour'] is not None}
            totals[key] = sum(hours.values())
            series[key] = [0] + [sum(n for h,n in hours.items() if h < edge.hour or edge == end and h == edge.hour) for edge in boundaries]

        measure('works', 'SELECT created_at AS stamp FROM videos')
        measure('comments', """SELECT MIN(julianday(stamp)) AS stamp FROM (
            SELECT external_id,page_url AS url,observed_at AS stamp FROM collection_observations
            WHERE kind='comment' AND trim(comment_text)!=''
            UNION ALL SELECT x.external_id,v.url,x.discovered_at FROM comments x JOIN videos v ON v.id=x.video_id
            ) GROUP BY external_id,url""")
        measure('live', 'SELECT observed_at AS stamp FROM live_messages')
        measure('groups', 'SELECT observed_at AS stamp FROM group_messages')
        measure('screened', """SELECT discovered_at AS stamp FROM comments UNION ALL
            SELECT observed_at FROM live_messages WHERE filter_reason='' UNION ALL
            SELECT observed_at FROM group_messages WHERE filter_reason=''""")
        measure('modeled', """SELECT MIN(julianday(finished_at)) AS stamp FROM intent_results
            WHERE method='model' AND status='completed' GROUP BY evidence_type,record_id""")
        measure('intent_users', """WITH identified AS (
            SELECT r.finished_at AS stamp,COALESCE(p.external_id,l.uid,g.uid) AS uid
            FROM intent_results r
            LEFT JOIN comments x ON r.evidence_type='comment' AND x.id=r.record_id
            LEFT JOIN people p ON p.id=x.person_id
            LEFT JOIN live_messages l ON r.evidence_type='live' AND l.id=r.record_id
            LEFT JOIN group_messages g ON r.evidence_type='group' AND g.id=r.record_id
            WHERE r.method='model' AND r.status='completed' AND json_valid(r.result_json)
            AND json_extract(r.result_json,'$.category')='buyer'
            AND json_extract(r.result_json,'$.game')='无畏契约'
            ) SELECT MIN(julianday(stamp)) AS stamp FROM identified WHERE uid IS NOT NULL AND trim(uid)!='' GROUP BY uid""")
        measure('dm_accepted', """SELECT m.created_at AS stamp FROM messages m
            JOIN uid_message_attempts a ON a.job_id=m.job_id WHERE m.direction='outbound'""")
        # Failure/unknown are today's submitted jobs' current outcomes; they are
        # operational counts, not a made-up immutable delivery event history.
        dm = dict(c.execute("""SELECT COUNT(*) AS attempted,
            COALESCE(SUM(status='failed'),0) AS failed,COALESCE(SUM(status='unknown'),0) AS unknown,
            COALESCE(SUM(status='submitting'),0) AS submitting FROM uid_message_attempts
            WHERE julianday(created_at)>=julianday(:start) AND julianday(created_at)<=julianday(:end)""", args).fetchone())
        measure('batches_completed', "SELECT finished_at AS stamp FROM collection_tasks WHERE status='completed'")
        measure('batches_partial', "SELECT finished_at AS stamp FROM collection_tasks WHERE status='partial'")
        measure('batches_error', """SELECT finished_at AS stamp FROM collection_tasks
            WHERE status NOT IN ('completed','partial','cancelled') AND finished_at IS NOT NULL""")
        captcha, captcha_events = captcha_summary(c, start, end)
        for key, passed_only in [('captcha_submitted',False),('captcha_passed',True)]:
            series[key] = [0] + [sum(stamp <= edge and (passed or not passed_only) for stamp,passed in captcha_events) for edge in boundaries]
        plan = c.execute('SELECT status,next_run_at,last_task_id FROM collection_plans WHERE continuous=1').fetchone()
        last = c.execute('SELECT id,status,detail,created_at,finished_at FROM collection_tasks ORDER BY id DESC LIMIT 1').fetchone()
        queued = c.execute("SELECT COUNT(*) FROM semantic_jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0]
        policy = c.execute("SELECT value FROM settings WHERE key='intent_outreach_policy'").fetchone()
        outreach = bool(json.loads(policy[0]).get('enabled')) if policy else False
    return dict(date=start.date().isoformat(), timezone='Asia/Shanghai', as_of=end.isoformat(),
                labels=['00:00']+[v.strftime('%H:%M') for v in boundaries],
                elapsed_hours=[0]+[(v-start).total_seconds()/3600 for v in boundaries],
                totals=totals, series=series, captcha=captcha, dm=dm,
                runtime=dict(monitor=dict(plan) if plan else None, latest_batch=dict(last) if last else None,
                             model_pending=queued, outreach_enabled=outreach),
                conversions=dict(official_account_follows=None,customer_service_adds=None,orders=None))


def workbench(mode='live'):
    dashboard = snapshot(mode)
    r = dashboard['runtime']
    return dict(mode=mode, dashboard=dashboard, settings={}, sources=[], videos=[], comments=[],
                live_messages=[], leads=[], members=[], jobs=[], messages=[], events=[], stats={},
                connections={'collector':'observed' if r['latest_batch'] else 'unverified','messaging':'not_connected'},
                collector=dict(tasks=[], plans=[], last_received=None, intent_outreach={'enabled':r['outreach_enabled']}))
