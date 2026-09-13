"""One normal follow in the existing backend profile, serialized with collection."""
import json,os,subprocess
import clubops as app
import collector


def follow(account,target):
    deferred=dict(status='deferred',proof=dict(submission_started=False,reason='browser_busy'))
    if not collector.GUARD.acquire(blocking=False):return deferred
    try:
        with app.db() as c:
            if c.execute("SELECT 1 FROM collection_tasks WHERE finished_at IS NULL AND status IN ('queued','starting','running') AND transport='local_browser' LIMIT 1").fetchone():return deferred
        node,package=collector.dependencies()
        if not node or not package.is_dir():return dict(status='not_submitted',proof=dict(submission_started=False,reason='browser_dependency'))
        env={**os.environ,'CLUBOPS_PLAYWRIGHT':str(package),'CLUBOPS_DATA_DIR':str(app.DATA_DIR)}
        try:
            process=subprocess.run([node,str(collector.BASE/'group_follow_browser.cjs')],
                input=json.dumps(dict(account=account,uid=target['uid'],sec_uid=target['sec_uid'])),
                text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=70,
                cwd=collector.BASE,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            value=json.loads(process.stdout)
            if value.get('status') not in ('accepted','uncertain','not_submitted') or not isinstance(value.get('proof'),dict):raise ValueError()
            return value
        except Exception:return dict(status='uncertain',proof=dict(submission_started=True,reason='browser_result_unknown'))
    finally:collector.GUARD.release()
