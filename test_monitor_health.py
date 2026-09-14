"""Health status semantics only: no service, platform, database or model requests."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest
import io
import json
import sqlite3
import tempfile
from unittest.mock import Mock,patch

spec = importlib.util.spec_from_file_location('monitor_health', Path(__file__).parent / 'scripts/monitor-health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)
NOW = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)


class HealthTests(unittest.TestCase):
    def test_pause_is_reported_without_recovery(self):
        plan = {'status': 'paused', 'last_task_id': 9}
        self.assertEqual(health.assess('comments', plan, None, NOW)['issue'], 'comments:paused')
        self.assertEqual(plan['status'], 'paused')

    def test_future_cooldown_is_not_stalled(self):
        plan = {'status': 'running', 'next_run_at': '2026-09-13T00:20:00Z', 'updated_at': '2026-09-12T20:00:00Z'}
        self.assertNotIn('issue', health.assess('comments', plan, {'finished_at': '2026-09-12T20:00:00Z'}, NOW))

    def test_failure_includes_task_reason_without_private_detail(self):
        result=health.assess('comments',{'status':'attention','last_task_id':2569},
            {'status':'needs_interaction','finished_at':'2026-09-13T00:00:00Z','detail':'PRIVATE_SENTINEL'},NOW)
        self.assertEqual(result['task_status'],'needs_interaction')
        self.assertEqual(result['task_finished_at'],'2026-09-13T00:00:00Z')
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(result))

    def test_running_task_has_its_own_progress_clock(self):
        plan = {'status': 'enabled', 'next_run_at': '2026-09-12T20:00:00Z'}
        task = {'updated_at': '2026-09-12T23:59:30Z', 'finished_at': None}
        self.assertNotIn('issue', health.assess('live', plan, task, NOW))
        task['updated_at'] = '2026-09-12T20:00:00Z'
        self.assertEqual(health.assess('live', plan, task, NOW)['issue'], 'live:task_stalled')

    def test_overdue_idle_scheduler(self):
        plan = {'status': 'running', 'next_run_at': '2026-09-12T23:40:00Z'}
        self.assertEqual(health.assess('comments', plan, None, NOW)['issue'], 'comments:scheduler_overdue')

    def test_inbox_failure_is_visible_but_manual_pause_and_future_retry_are_not_faults(self):
        rows=[dict(id=1,enabled=0,status='attention',updated_at='2026-09-12T20:00:00Z'),
              dict(id=2,enabled=0,status='paused'),
              dict(id=3,enabled=1,status='retry_wait',next_run_at='2026-09-13T00:01:00Z')]
        result=health.assess_inbox(rows,NOW)
        self.assertEqual(result['enabled'],1);self.assertEqual(result['paused'],1)
        self.assertEqual(result['issue_count'],1);self.assertEqual(result['issues'],['inbox_sync:1:attention'])
        self.assertEqual(rows[0]['status'],'attention')

    def test_inbox_stalled_reads_and_missing_schedule_are_detected_with_bounded_output(self):
        rows=[dict(id=1,enabled=1,status='reading',updated_at='2026-09-12T23:55:00Z'),
              dict(id=2,enabled=1,status='waiting',next_run_at=None),
              dict(id=3,enabled=1,status='catching_up',next_run_at='2026-09-12T23:59:00Z')]
        result=health.assess_inbox(rows,NOW)
        self.assertEqual(result['issues'],['inbox_sync:1:read_stalled','inbox_sync:2:schedule_missing'])
        many=health.assess_inbox([dict(id=n,enabled=0,status='attention') for n in range(1,101)],NOW)
        self.assertEqual(many['issue_count'],100);self.assertEqual(len(many['issues']),20)
        self.assertEqual(health.assess_inbox([],NOW)['issue_count'],0)

    def test_check_reads_all_configured_inboxes_without_business_mutation_or_platform_access(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'clubops-live.db'
            c=sqlite3.connect(path)
            c.executescript('''
              CREATE TABLE collection_plans(id INTEGER,status TEXT,continuous INTEGER,last_task_id INTEGER,next_run_at TEXT);
              CREATE TABLE collection_tasks(id INTEGER,finished_at TEXT);
              CREATE TABLE live_tracks(id INTEGER,status TEXT,last_session_id INTEGER,next_run_at TEXT);
              CREATE TABLE live_sessions(id INTEGER,finished_at TEXT);
              CREATE TABLE uid_inbox_sync(id INTEGER,enabled INTEGER,status TEXT,next_run_at TEXT,updated_at TEXT,detail TEXT);
              INSERT INTO collection_plans VALUES(1,'running',1,NULL,'2099-01-01T00:00:00Z');
              INSERT INTO live_tracks VALUES(1,'enabled',NULL,'2099-01-01T00:00:00Z');
              INSERT INTO uid_inbox_sync VALUES(1,0,'attention',NULL,'2026-09-12T20:00:00Z','synthetic-private-profile');
              INSERT INTO uid_inbox_sync VALUES(2,1,'waiting','2099-01-01T00:00:00Z','2026-09-12T20:00:00Z','');
            ''');c.commit();c.close();before=path.read_bytes()
            opener=Mock();opener.open.return_value=io.BytesIO(json.dumps({'status':'running'}).encode())
            with patch.object(health.urllib.request,'build_opener',return_value=opener):result=health.check(folder,8765)
            self.assertEqual(result['status'],'attention');self.assertIn('inbox_sync:1:attention',result['issues'])
            self.assertEqual(result['feedback'],{'mode':'local_only','codex_push_connected':False})
            self.assertEqual(result['inbox_sync']['configured'],2)
            opener.open.assert_called_once_with('http://127.0.0.1:8765/api/service',timeout=5)
            self.assertNotIn('synthetic-private-profile',json.dumps(result));self.assertEqual(path.read_bytes(),before)
