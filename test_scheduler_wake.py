"""The collector's completion signal wakes decisions without waiting for a timer."""
import threading
import unittest
from unittest.mock import patch
import collection_scheduler as scheduler


class SchedulerWakeTests(unittest.TestCase):
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
