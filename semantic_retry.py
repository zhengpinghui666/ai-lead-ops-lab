"""Bounded retries for unfinished model work, with the original evidence guards."""
import json
from datetime import datetime, timedelta
import analysis_store as store
import asset_verticality
import demand_freshness
import semantic

SCHEMA = '''
CREATE TABLE IF NOT EXISTS semantic_job_retries (
 job_id INTEGER PRIMARY KEY REFERENCES semantic_jobs(id), retries INTEGER NOT NULL DEFAULT 0,
 source_result_id INTEGER, due_at TEXT, scheduled INTEGER NOT NULL DEFAULT 0
);
'''
DELAYS = (60, 240)


def eligible(c, job, settings, engine, stamp):
    if job['engine'] != engine or semantic.effective_config(json.loads(job['config_json']),job['evidence_type']) != semantic.effective_config(settings,job['evidence_type']):return False
    kind, record_id = job['evidence_type'],job['record_id']
    if kind=='live' and not settings['live_model_enabled']:return False
    source,row=store.inputs(c,kind,record_id)
    if row['analysis_method']!='rules' or store.digest(source)!=job['input_hash']:return False
    if kind=='live' and c.execute('SELECT 1 FROM live_judgments WHERE message_id=?',(record_id,)).fetchone():return False
    if not demand_freshness.record(c,kind,record_id,now=stamp)['eligible']:return False
    if any(len(source[k])>5000 for k in ('text','parent','title')):return False
    if not asset_verticality.routing(c,kind,record_id,refresh_asset=False)['model_allowed']:return False
    result=store.latest(c,kind,record_id,'model',job['input_hash'])
    if job['result_id'] is None:
        # Recovery of the old 1,000-character description guard: no model
        # request was created. Do not retry other unknown local exceptions.
        return result is None and 1000<len(source['title'])<=5000
    if not result or result['id']!=job['result_id'] or result['status']!='failed' or not store.compatible_engine(result['engine'],engine):return False
    diagnostic=result['result'].get('diagnostic',{})
    code,status=diagnostic.get('code'),diagnostic.get('http_status')
    if status is not None and status not in (200,500,502,503,504):return False
    return code in ('timeout','connection','dns') or code=='api_server' and status in (500,502,503,504)


def promote(c, settings, channel, stamp, *, capacity=200):
    """Called under the queue lock. Retry counts survive restarts and polling."""
    if not settings['enabled'] or not settings['auto_analyze'] or not channel['can_analyze']:return 0
    pending=c.execute("SELECT COUNT(*) FROM semantic_jobs WHERE status IN ('queued','running','cancelling')").fetchone()[0]
    rows=c.execute("""SELECT j.* FROM semantic_jobs j LEFT JOIN semantic_job_retries r ON r.job_id=j.id
      WHERE j.status='failed' AND j.engine=? AND COALESCE(r.retries,0)<2
        AND julianday(j.finished_at)>=julianday(?)-1
      ORDER BY j.id DESC LIMIT 100""",(channel['engine'],stamp.isoformat())).fetchall()
    promoted=0
    for job in rows:
        retry=c.execute('SELECT * FROM semantic_job_retries WHERE job_id=?',(job['id'],)).fetchone()
        try:ok=eligible(c,job,settings,channel['engine'],stamp)
        except (ValueError,TypeError,KeyError):ok=False
        if not ok:
            if retry and retry['scheduled']:c.execute('UPDATE semantic_job_retries SET scheduled=0,due_at=NULL WHERE job_id=?',(job['id'],))
            continue
        attempts=retry['retries'] if retry else 0
        due=datetime.fromisoformat(job['finished_at'])+timedelta(seconds=DELAYS[attempts])
        if not retry or not retry['scheduled'] or retry['source_result_id']!=job['result_id']:
            c.execute('''INSERT INTO semantic_job_retries(job_id,retries,source_result_id,due_at,scheduled) VALUES(?,?,?,?,1)
              ON CONFLICT(job_id) DO UPDATE SET source_result_id=excluded.source_result_id,due_at=excluded.due_at,scheduled=1''',
              (job['id'],attempts,job['result_id'],due.isoformat()))
        if stamp<due or pending>=capacity:continue
        c.execute("UPDATE semantic_jobs SET status='queued',started_at=NULL,finished_at=NULL,detail=? WHERE id=?",
                  (f'自动重试 {attempts+1}/2 已入队；保留此前失败记录',job['id']))
        c.execute('UPDATE semantic_job_retries SET retries=retries+1,scheduled=0,due_at=NULL WHERE job_id=?',(job['id'],))
        pending+=1;promoted+=1
    return promoted
