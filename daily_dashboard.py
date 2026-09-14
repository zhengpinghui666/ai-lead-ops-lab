"""Read-only daily aggregates. Event time, not the current size of lead lists.

No customer text, model inputs, credentials or remote calls enter this response.
Captcha submission counters are cumulative within a batch, not per event ID.
"""
from datetime import datetime, timedelta, timezone
import json
import clubops as app

BEIJING = timezone(timedelta(hours=8))
CAPTCHA_TYPES = {'slider':'滑块拼图','same_shape':'同形点选','sms':'短信验证码','other':'其他／未识别类型'}


def parsed(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=BEIJING)
    except (ValueError, TypeError):
        return None


def captcha_summary(c, start, end, event_start=None):
    batches = {}
    for row in c.execute("SELECT task_id,created_at,snapshot FROM collection_diagnostics WHERE stage='captcha_workflow' ORDER BY id"):
        stamp = parsed(row['created_at'])
        if not stamp or stamp > end:
            continue
        try:
            v = json.loads(row['snapshot'])['verification']
        except (ValueError, KeyError, TypeError):
            continue
        b = batches.setdefault(row['task_id'], dict(at=stamp, submitted_at=None, passed=False, failed=False, legacy=False,kind='other'))
        b['at'] = min(b['at'], stamp)
        adapter=v.get('adapter','')
        if adapter=='douyin_same_shape_pair':b['kind']='same_shape'
        elif adapter in ('douyin_iframe_slider','legacy_slider_dom','douyin_embedded_dom') and (v.get('submissions')==1 or v.get('phase') in ('recognizing','submitting','verifying','accepted')):b['kind']='slider'
        if v.get('submissions') == 1:
            b['submitted_at'] = min(b['submitted_at'] or stamp, stamp)
        if v.get('phase') == 'accepted':
            if v.get('submissions') == 1 and v.get('platform_verdict') == 'passed' and v.get('verdict_source') in ('visible_platform_result', 'platform_response_message'):
                b['passed'] = True  # The producer emits accepted only after a fresh valid data response.
            else:
                b['legacy'] = True
        if v.get('platform_verdict') == 'failed':
            b['failed'] = True

    for row in c.execute('SELECT * FROM login_verification_metrics'):
        at=parsed(row['encountered_at']);submitted=parsed(row['submitted_at']);passed_at=parsed(row['passed_at'])
        if not at or at>end:continue
        batches['sms:'+row['job_id']]=dict(at=at,submitted_at=submitted,passed=bool(passed_at and passed_at<=end),failed=False,legacy=False,kind='sms',time_basis=row['time_basis'])

    def summarize(begin,finish=end,kind=None):
        selected=[b for b in batches.values() if kind is None or b['kind']==kind]
        encounters = [b for b in selected if begin <= b['at'] <= finish]
        submitted = [b for b in selected if b['submitted_at'] and begin <= b['submitted_at'] <= finish]
        passed = sum(b['passed'] for b in submitted)
        failed = sum(b['failed'] and not b['passed'] for b in submitted)
        return dict(encounters=len(encounters), submitted=len(submitted), passed=passed,
                    failed=failed, unknown=len(submitted)-passed-failed,
                    not_submitted=sum(b['submitted_at'] is None for b in encounters),
                    legacy_accepted=sum(b['legacy'] and not b['passed'] for b in submitted),
                    pass_rate=round(passed/len(submitted)*100, 1) if submitted else None)
    today = summarize(start)
    today['all_time'] = summarize(datetime.min.replace(tzinfo=timezone.utc))
    today['types']=[dict(key=key,name=name,**summarize(start,kind=key)) for key,name in CAPTCHA_TYPES.items()]
    today['daily']=[]
    day=(event_start or start).astimezone(BEIJING)
    while day<=end:
        finish=min(end,day+timedelta(days=1)-timedelta(microseconds=1))
        today['daily'].append(dict(date=day.date().isoformat(),types=[dict(key=key,name=name,**summarize(day,finish,key)) for key,name in CAPTCHA_TYPES.items()]))
        day+=timedelta(days=1)
    today['historical_sms_time_count']=sum(b.get('time_basis')=='historical_result_time' for b in batches.values())
    events = [(b['submitted_at'], b['passed']) for b in batches.values()
              if b['submitted_at'] and (event_start or start) <= b['submitted_at'] <= end]
    return today, events


def snapshot(mode='live', reference=None):
    end = (reference or datetime.now(timezone.utc)).astimezone(BEIJING)
    start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    history_start = start - timedelta(days=89)
    dates = [(history_start + timedelta(days=i)).date().isoformat() for i in range(90)]
    series = {}
    totals = {}
    with app.db(mode) as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('PRAGMA temp_store=MEMORY')
        c.execute('BEGIN')  # One WAL read snapshot, without taking the collector's writer lock.
        args = dict(start=start.isoformat(), end=end.isoformat(), history_start=history_start.isoformat())

        def measure(key, source):
            # Group first events by Beijing calendar day, then fill quiet days.
            # Daily values are not running totals and today's card stays today-only.
            rows = c.execute(f"""SELECT date(stamp,'+8 hours') AS day,COUNT(*) AS n
                FROM ({source}) WHERE julianday(stamp)>=julianday(:history_start)
                AND julianday(stamp)<=julianday(:end) GROUP BY day""", args).fetchall()
            days = {r['day']:r['n'] for r in rows if r['day'] is not None}
            totals[key] = days.get(dates[-1], 0)
            series[key] = [days.get(day, 0) for day in dates]

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
        captcha, captcha_events = captcha_summary(c, start, end, history_start)
        for key in CAPTCHA_TYPES:
            series['captcha_rate_'+key]=[next(t['pass_rate'] for t in day['types'] if t['key']==key) for day in captcha['daily']]
        first_captcha=next((i for i,day in enumerate(captcha['daily']) if any(t['encounters'] or t['submitted'] for t in day['types'])),None)
        captcha['daily']=captcha['daily'][first_captcha:] if first_captcha is not None else []
        for key, passed_only in [('captcha_submitted',False),('captcha_passed',True)]:
            days = {}
            for stamp, passed in captcha_events:
                if passed_only and not passed:
                    continue
                day = stamp.astimezone(BEIJING).date().isoformat()
                days[day] = days.get(day, 0) + 1
            series[key] = [days.get(day, 0) for day in dates]
        plan = c.execute('SELECT status,next_run_at,last_task_id FROM collection_plans WHERE continuous=1').fetchone()
        last = c.execute('SELECT id,status,detail,created_at,finished_at FROM collection_tasks ORDER BY id DESC LIMIT 1').fetchone()
        last=dict(last) if last else None
        if last:
            import collection_accounts
            last['collection_account']=collection_accounts.binding(c,last['id'])
            verification=c.execute("SELECT snapshot FROM collection_diagnostics WHERE task_id=? AND stage='captcha_workflow' ORDER BY id DESC LIMIT 1",(last['id'],)).fetchone()
            if verification:
                try:
                    raw=json.loads(verification[0]).get('verification',{})
                    last['verification']={key:raw[key] for key in ('phase','adapter','reason','submissions') if key in raw}
                except (ValueError,TypeError):pass
        queued = c.execute("SELECT COUNT(*) FROM semantic_jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0]
        policy = c.execute("SELECT value FROM settings WHERE key='intent_outreach_policy'").fetchone()
        outreach = bool(json.loads(policy[0]).get('enabled')) if policy else False
    import incident_bridge
    feedback=incident_bridge.feedback(app.DATA_DIR) if mode=='live' else dict(mode='demo',realtime_connected=False)
    return dict(date=start.date().isoformat(), timezone='Asia/Shanghai', as_of=end.isoformat(),
                labels=dates, granularity='day', history_days=90,
                totals=totals, series=series, captcha=captcha, dm=dm,
                runtime=dict(monitor=dict(plan) if plan else None, latest_batch=dict(last) if last else None,
                             model_pending=queued, outreach_enabled=outreach, feedback=feedback),
                conversions=dict(official_account_follows=None,customer_service_adds=None,orders=None))


def workbench(mode='live'):
    dashboard = snapshot(mode)
    r = dashboard['runtime']
    return dict(mode=mode, dashboard=dashboard, settings={}, sources=[], videos=[], comments=[],
                live_messages=[], leads=[], members=[], jobs=[], messages=[], events=[], stats={},
                connections={'collector':'observed' if r['latest_batch'] else 'unverified','messaging':'not_connected'},
                collector=dict(tasks=[], plans=[], last_received=None, intent_outreach={'enabled':r['outreach_enabled']}))
