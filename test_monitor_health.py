"""Health status semantics only: no service, platform, database or model requests."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest

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

    def test_running_task_has_its_own_progress_clock(self):
        plan = {'status': 'enabled', 'next_run_at': '2026-09-12T20:00:00Z'}
        task = {'updated_at': '2026-09-12T23:59:30Z', 'finished_at': None}
        self.assertNotIn('issue', health.assess('live', plan, task, NOW))
        task['updated_at'] = '2026-09-12T20:00:00Z'
        self.assertEqual(health.assess('live', plan, task, NOW)['issue'], 'live:task_stalled')

    def test_overdue_idle_scheduler(self):
        plan = {'status': 'running', 'next_run_at': '2026-09-12T23:40:00Z'}
        self.assertEqual(health.assess('comments', plan, None, NOW)['issue'], 'comments:scheduler_overdue')
