"""Read-only, bounded local monitoring checks; never launches collection or sends messages."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import urllib.request

BASE = Path(__file__).resolve().parents[1]


def overdue(value, now, grace=600):
    if not value:
        return False
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (now - stamp).total_seconds() > grace


def assess(name, plan, task, now):
    if not plan:
        return {'state': 'unconfigured', 'issue': name + ':unconfigured'}
    enabled = plan['status'] == ('running' if name == 'comments' else 'enabled')
    result = {'state': plan['status'], 'last_task_id': plan.get('last_task_id', plan.get('last_session_id'))}
    if task:
        result.update(task_status=task.get('status'),task_finished_at=task.get('finished_at'))
    if not enabled:
        result['issue'] = name + ':' + plan['status']
    elif task and not task.get('finished_at'):
        if overdue(task.get('updated_at') or task.get('started_at'), now):
            result['issue'] = name + ':task_stalled'
    elif overdue(plan.get('next_run_at') or plan.get('updated_at'), now):
        result['issue'] = name + ':scheduler_overdue'
    return result


def assess_inbox(rows, now):
    result = {'configured': len(rows), 'enabled': 0, 'paused': 0, 'issue_count': 0, 'issues': []}
    for row in rows:
        enabled, status = bool(row['enabled']), row['status']
        result['enabled'] += int(enabled)
        result['paused'] += int(not enabled and status == 'paused')
        reason = None
        if status == 'attention':
            reason = 'attention'
        elif enabled:
            if status == 'reading':
                stamp, missing, stale = row.get('updated_at'), 'progress_missing', 'read_stalled'
            elif status in {'waiting', 'catching_up', 'retry_wait'}:
                stamp, missing, stale = row.get('next_run_at'), 'schedule_missing', 'scheduler_overdue'
            else:
                stamp, missing, stale = None, 'unexpected_state', 'unexpected_state'
            try:
                if not stamp:
                    reason = missing
                elif overdue(stamp, now, grace=120):
                    reason = stale
            except (ValueError, TypeError):
                reason = 'invalid_clock'
        if reason:
            result['issue_count'] += 1
            if len(result['issues']) < 20:
                result['issues'].append(f"inbox_sync:{row['id']}:{reason}")
    return result


def check(data_dir=BASE / 'data', port=8765):
    now = datetime.now(timezone.utc)
    # A fresh listener heartbeat alone is not proof that Codex received a message.
    import sys
    if str(BASE) not in sys.path:sys.path.insert(0,str(BASE))
    from incident_bridge import feedback
    report = {'checked_at': now.isoformat(), 'status': 'healthy', 'issues': [],
              'feedback': feedback(data_dir)}
    if report['feedback']['mode']=='event_queue':
        if not report['feedback'].get('watcher_running'):
            report['issues'].append('feedback:watcher_stale')
        if (report['feedback'].get('last_delivery') or {}).get('status')=='unknown':
            report['issues'].append('feedback:delivery_unknown')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/api/service', timeout=5) as response:
            service = json.loads(response.read(65537))
        report['service'] = service['status']
        if service['status'] != 'running':
            report['issues'].append('service:' + service['status'])
        access = service.get('external_access', {})
        if access.get('enabled') and not access.get('connected'):
            report['issues'].append('external_access:disconnected')
        path = Path(data_dir).resolve() / 'clubops-live.db'
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + 3
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute('BEGIN')
            for name, table, task_table, task_key, where in (
                ('comments', 'collection_plans', 'collection_tasks', 'last_task_id', 'WHERE continuous=1'),
                ('live', 'live_tracks', 'live_sessions', 'last_session_id', ''),
            ):
                row = connection.execute(f'SELECT * FROM {table} {where} ORDER BY id DESC LIMIT 1').fetchone()
                plan = dict(row) if row else None
                row = connection.execute(f'SELECT * FROM {task_table} WHERE id=?', (plan[task_key],)).fetchone() if plan else None
                item = assess(name, plan, dict(row) if row else None, now)
                report[name] = item
                if item.get('issue'):
                    report['issues'].append(item['issue'])
            rows = [dict(row) for row in connection.execute('''SELECT id,enabled,status,next_run_at,updated_at
                FROM uid_inbox_sync ORDER BY id''')]
            report['inbox_sync'] = assess_inbox(rows, now)
            report['issues'].extend(report['inbox_sync']['issues'])
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='monitored_groups'").fetchone():
                groups=[dict(r) for r in connection.execute('SELECT id,enabled,status,failures,next_run_at,last_read_at FROM monitored_groups')]
                report['groups']={'configured':len(groups),'enabled':sum(r['enabled'] for r in groups)}
                for group in groups:
                    if group['enabled'] and (group['failures']>=3 or overdue(group['next_run_at'],now,120)
                            or group['last_read_at'] and overdue(group['last_read_at'],now,600)):
                        report['issues'].append(f"groups:{group['id']}:{group['status']}")
    except Exception as error:
        # Do not serialize exceptions containing response bodies or account data.
        report['issues'].append('probe:' + type(error).__name__)
    if report['issues']:
        report['status'] = 'attention'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true', help='Check every 60 seconds without model calls')
    args = parser.parse_args()
    output = BASE / 'data' / 'monitor-health.json'
    while True:
        report = check()
        if not args.watch:
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report['status'] == 'healthy' else 1
        temp = output.with_suffix('.tmp')
        temp.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        temp.replace(output)
        time.sleep(60)


if __name__ == '__main__':
    raise SystemExit(main())
