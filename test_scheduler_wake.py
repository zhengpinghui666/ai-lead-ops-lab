"""The collector's completion signal wakes decisions without waiting for a timer."""
import threading
import unittest
from unittest.mock import patch
import collection_scheduler as scheduler


class SchedulerWakeTests(unittest.TestCase):
    def test_successful_due_work_has_no_shared_thirty_second_idle(self):
        plan=dict(continuous=1,interval_seconds=30)
        task=dict(status='completed',transport='http',kind='video',finished_at='2026-09-15T08:00:02+00:00')
        for lane in ('front','history'):
            self.assertEqual(scheduler.completed_next_run(plan,task,dict(channel='work',read_lane=lane)),task['finished_at'])
        for changed,job in [(dict(plan,continuous=0),dict(channel='work',read_lane='front')),(plan,None),(plan,dict(channel='search',read_lane='front'))]:
            self.assertEqual(scheduler.completed_next_run(changed,task,job),'2026-09-15T08:00:32+00:00')
        self.assertEqual(scheduler.completed_next_run(plan,dict(task,kind='author'),dict(channel='work',read_lane='front')),'2026-09-15T08:00:32+00:00')
        for kind,transport,channel in [('author','http','author'),('search','local_browser','search'),('search','http','search')]:
            self.assertEqual(scheduler.completed_next_run(plan,dict(task,kind=kind,transport=transport),dict(channel=channel)),task['finished_at'])

    def test_failure_or_platform_gate_cannot_use_success_cadence(self):
        for status in ('network_error','needs_verification','rate_limited','needs_login','partial','cancelled'):
            with self.subTest(status=status),self.assertRaises(ValueError):
                scheduler.completed_next_run(dict(continuous=1,interval_seconds=30),dict(status=status,transport='http',kind='video',finished_at='2026-09-15T08:00:02+00:00'),dict(channel='work',read_lane='front'))

    def test_completion_wakes_idle_scheduler_and_shutdown_does_not_dispatch(self):
        called=threading.Event()
        self.assertFalse(scheduler.THREAD and scheduler.THREAD.is_alive())
        scheduler.WAKE.clear()
        with patch.object(scheduler,'tick',side_effect=called.set) as tick,patch.object(scheduler,'recover'):
            scheduler.start_service()
            try:
                self.assertFalse(called.is_set())
                scheduler.notify_finished()
                self.assertTrue(called.wait(1),'Completion should wake before the three-second fallback')
            finally:
                scheduler.shutdown()
            self.assertEqual(tick.call_count,1,'Shutdown wake must not dispatch new work')


if __name__=='__main__':unittest.main()
